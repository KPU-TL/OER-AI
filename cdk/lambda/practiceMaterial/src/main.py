import os
import json
import time
import logging
import boto3
import psycopg2
from typing import Any, Dict
# import helpers
from helpers.vectorstore import get_textbook_retriever
from helpers.cache_manager import generate_cache_key, get_cached_response, set_cached_response
from langchain_aws import BedrockEmbeddings, ChatBedrock
# practice material grading handler
from generators.mcq import build_mcq_prompt, validate_mcq_shape
from generators.flashcard import build_flashcard_prompt, validate_flashcard_shape
from generators.short_answer import build_short_answer_prompt, validate_short_answer_shape, build_grading_prompt
# Set up logging - Lambda pre-configures root logger, so we need to set level explicitly
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Environment variables
REGION = os.environ.get("REGION", "ca-central-1")
SM_DB_CREDENTIALS = os.environ.get("SM_DB_CREDENTIALS", "")
RDS_PROXY_ENDPOINT = os.environ.get("RDS_PROXY_ENDPOINT", "")
PRACTICE_MATERIAL_MODEL_PARAM = os.environ.get("PRACTICE_MATERIAL_MODEL_PARAM")
EMBEDDING_MODEL_PARAM = os.environ.get("EMBEDDING_MODEL_PARAM")
BEDROCK_REGION_PARAM = os.environ.get("BEDROCK_REGION_PARAM")
GUARDRAIL_ID_PARAM = os.environ.get("GUARDRAIL_ID_PARAM")
COLD_START_METRIC = os.environ.get("COLD_START_METRIC", "false").lower() == "true"

# AWS Clients
secrets_manager = boto3.client("secretsmanager", region_name=REGION)
ssm_client = boto3.client("ssm", region_name=REGION)
bedrock_runtime = boto3.client("bedrock-runtime", region_name='us-east-1')  # For embeddings (Cohere is in us-east-1)

# Cache for secrets and connections
_db_secret: Dict[str, Any] | None = None
_db_connection = None  # Cached connection (RDS Proxy handles pooling)
_embeddings = None
_llm = None
_is_cold_start = True

# Pre-loaded configuration - loaded at container startup (outside handler)
PRACTICE_MATERIAL_MODEL_ID: str | None = None
EMBEDDING_MODEL_ID: str | None = None
BEDROCK_REGION: str | None = None
GUARDRAIL_ID: str | None = None

# Pre-load critical configuration during container startup
try:
    logger.info("Pre-loading critical configuration...")
    
    # Pre-fetch SSM parameters
    if PRACTICE_MATERIAL_MODEL_PARAM:
        PRACTICE_MATERIAL_MODEL_ID = ssm_client.get_parameter(Name=PRACTICE_MATERIAL_MODEL_PARAM, WithDecryption=True)["Parameter"]["Value"]
        logger.info(f"Pre-loaded PRACTICE_MATERIAL_MODEL_ID: {PRACTICE_MATERIAL_MODEL_ID}")
    
    if EMBEDDING_MODEL_PARAM:
        EMBEDDING_MODEL_ID = ssm_client.get_parameter(Name=EMBEDDING_MODEL_PARAM, WithDecryption=True)["Parameter"]["Value"]
        logger.info(f"Pre-loaded EMBEDDING_MODEL_ID: {EMBEDDING_MODEL_ID}")
    
    if BEDROCK_REGION_PARAM:
        BEDROCK_REGION = ssm_client.get_parameter(Name=BEDROCK_REGION_PARAM, WithDecryption=True)["Parameter"]["Value"]
        logger.info(f"Pre-loaded BEDROCK_REGION: {BEDROCK_REGION}")
    else:
        BEDROCK_REGION = REGION
        logger.info(f"Using deployment region as BEDROCK_REGION: {BEDROCK_REGION}")
    
    if GUARDRAIL_ID_PARAM:
        GUARDRAIL_ID = ssm_client.get_parameter(Name=GUARDRAIL_ID_PARAM, WithDecryption=True)["Parameter"]["Value"]
        logger.info(f"Pre-loaded GUARDRAIL_ID")
    
    logger.info("Pre-loading completed successfully")
except Exception as e:
    logger.warning(f"Pre-loading failed (will load on-demand): {e}")



def emit_cold_start_metrics(function_name: str, execution_ms: int, cold_start_ms: int | None) -> None:
    """Emit embedded CloudWatch metrics for cold start and execution time."""
    if not COLD_START_METRIC:
        return

    payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": "Lambda/ColdStart",
                    "Dimensions": [["FunctionName"]],
                    "Metrics": [
                        {"Name": "ColdStart", "Unit": "Count"},
                        {"Name": "ColdStartDurationMs", "Unit": "Milliseconds"},
                        {"Name": "ExecutionTimeMs", "Unit": "Milliseconds"},
                    ],
                }
            ],
        },
        "FunctionName": function_name,
        "ColdStart": 1 if cold_start_ms is not None else 0,
        "ColdStartDurationMs": cold_start_ms or 0,
        "ExecutionTimeMs": execution_ms,
    }

    print(json.dumps(payload))


def send_websocket_progress(
    connection_id: str | None,
    domain_name: str | None,
    stage: str | None,
    status: str,
    progress: int,
    data: Dict[str, Any] | None = None,
    error: str | None = None
) -> None:
    """
    Send progress updates to the client via WebSocket.
    
    Args:
        connection_id: WebSocket connection ID
        domain_name: API Gateway domain name
        stage: API Gateway stage
        status: Status message (e.g., 'initializing', 'retrieving', 'generating', 'complete', 'error')
        progress: Progress percentage (0-100)
        data: Optional data payload (for 'complete' status)
        error: Optional error message (for 'error' status)
    """
    if not connection_id or not domain_name or not stage:
        logger.debug("No WebSocket context, skipping progress update")
        return
    
    try:
        endpoint_url = f"https://{domain_name}/{stage}"
        apigw_management = boto3.client(
            "apigatewaymanagementapi",
            endpoint_url=endpoint_url,
            region_name=REGION
        )
        
        message = {
            "type": "practice_material_progress",
            "status": status,
            "progress": progress,
        }
        
        if data is not None:
            message["data"] = data
        if error is not None:
            message["error"] = error
        
        apigw_management.post_to_connection(
            ConnectionId=connection_id,
            Data=json.dumps(message).encode("utf-8")
        )
        logger.info(f"Sent WebSocket progress: status={status}, progress={progress}%")
    except Exception as e:
        logger.warning(f"Failed to send WebSocket progress: {e}")
        # Don't fail the request if WebSocket update fails


def get_secret_dict(name: str) -> Dict[str, Any]:
    global _db_secret
    if _db_secret is None:
        val = secrets_manager.get_secret_value(SecretId=name)["SecretString"]
        _db_secret = json.loads(val)
    return _db_secret


def get_db_connection():
    """
    Get or create a database connection (RDS Proxy handles pooling).
    Connection is reused across Lambda invocations for better performance.
    
    If the connection fails due to stale credentials (e.g., after rotation),
    the cached secret is cleared and a retry is attempted with fresh credentials.
    """
    global _db_connection, _db_secret
    
    # Check if connection exists and is still valid
    if _db_connection is not None:
        try:
            # Test if connection is still alive
            with _db_connection.cursor() as cur:
                cur.execute("SELECT 1")
            return _db_connection
        except Exception:
            logger.warning("Existing connection is stale, creating new one")
            try:
                _db_connection.close()
            except Exception:
                pass
            _db_connection = None
    
    # Create new connection (with one retry on auth failure for rotated credentials)
    for attempt in range(2):
        logger.info(f"Creating new database connection (attempt {attempt + 1}/2)")
        db = get_secret_dict(SM_DB_CREDENTIALS)
        
        try:
            _db_connection = psycopg2.connect(
                dbname=db["dbname"],
                user=db["username"],
                password=db["password"],
                host=RDS_PROXY_ENDPOINT,
                port=db["port"]
            )
            logger.info("Database connection created successfully")
            return _db_connection
        except psycopg2.OperationalError as e:
            if attempt == 0:
                logger.warning(f"Database connection failed (possibly stale credentials), clearing cache and retrying: {e}")
                _db_secret = None  # Clear cached secret to force fresh fetch
            else:
                logger.error(f"Database connection failed after retry with fresh credentials: {e}")
                raise
        except Exception as e:
            logger.error(f"Failed to create database connection: {e}")
            raise


def initialize_constants():
    """Initialize LLM and embeddings using pre-loaded configuration.
    
    This function is now mostly for lazy-loading LLM and embeddings.
    SSM parameters are pre-loaded at module import time.
    """
    global _embeddings, _llm
    
    # Verify pre-loaded configuration
    if PRACTICE_MATERIAL_MODEL_ID is None:
        logger.warning("PRACTICE_MATERIAL_MODEL_ID not pre-loaded")
        return
    
    if EMBEDDING_MODEL_ID is None:
        logger.warning("EMBEDDING_MODEL_ID not pre-loaded")
        return
    
    logger.info(f"Using pre-loaded configuration - LLM: {PRACTICE_MATERIAL_MODEL_ID}, Embeddings: {EMBEDDING_MODEL_ID}, Region: {BEDROCK_REGION}")
    
    # Initialize embeddings if not already done
    if _embeddings is None:
        _embeddings = BedrockEmbeddings(
            model_id=EMBEDDING_MODEL_ID,
            client=bedrock_runtime,
            region_name='us-east-1',
            model_kwargs = {"input_type": "search_query"}
        )
        logger.info("Embeddings initialized successfully")
    
    # Initialize LLM if not already done
    if _llm is None:
        # Create bedrock client for LLM in the appropriate region
        llm_client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
        model_kwargs = {
            "temperature": 0.6,
            "max_tokens": 4096,
            "top_p": 0.9,
        }
        logger.info(f"Creating ChatBedrock instance for model: {PRACTICE_MATERIAL_MODEL_ID}")
        logger.info(f"Model parameters: {json.dumps(model_kwargs)}")
        
        _llm = ChatBedrock(
            model_id=PRACTICE_MATERIAL_MODEL_ID,
            model_kwargs=model_kwargs,
            client=llm_client
        )
        logger.info("ChatBedrock LLM initialized successfully")


def apply_guardrails(text: str, source: str = "INPUT") -> dict:
    """Apply Bedrock guardrails to input or output text.
    
    SECURITY: Uses fail-closed model - blocks content when guardrails fail.
    
    Args:
        text: The text to check against guardrails
        source: Either "INPUT" or "OUTPUT"
        
    Returns:
        dict with 'blocked', 'action', and 'assessments' keys
    """
    
    if not GUARDRAIL_ID:
        logger.debug("No guardrail ID configured, skipping guardrail check")
        return {'blocked': False, 'action': 'NONE', 'assessments': []}
    
    try:
        # Create bedrock client in the correct region for guardrails
        bedrock_client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
        response = bedrock_client.apply_guardrail(
            guardrailIdentifier=GUARDRAIL_ID,
            guardrailVersion="DRAFT",
            source=source,
            content=[{"text": {"text": text}}]
        )
        
        action = response.get('action', 'NONE')
        blocked = action == 'GUARDRAIL_INTERVENED'
        
        if blocked:
            logger.warning(f"SECURITY: Guardrail blocked {source}: action={action}")
        
        return {
            'blocked': blocked,
            'action': action,
            'assessments': response.get('assessments', [])
        }
    except Exception as e:
        logger.error(f"SECURITY ALERT: Guardrail check failed: {e}")
        # SECURITY: Fail-closed - block content when guardrails fail
        return {
            'blocked': True,
            'action': 'GUARDRAIL_ERROR',
            'assessments': [],
            'error': str(e)
        }



def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))





def extract_sources_from_docs(docs) -> list[str]:
    """
    Extract source citations from document objects.
    Mirrors the function from textGeneration/src/helpers/chat.py
    """
    sources_used = []
    for doc in docs:
        if hasattr(doc, "metadata"):
            source = doc.metadata.get("source", "")
            page = doc.metadata.get("page", None)
            if source and source not in sources_used:
                source_entry = f"{source}"
                if page:
                    source_entry += f" (p. {page})"
                sources_used.append(source_entry)
    return sources_used


def track_practice_material_analytics(
    textbook_id: str,
    material_type: str,
    topic: str,
    num_items: int,
    difficulty: str,
    metadata: Dict[str, Any],
    user_session_id: str = None
):
    """
    Insert a record into practice_material_analytics table to track generation.
    
    Args:
        textbook_id: UUID of the textbook
        material_type: Type of material ('mcq', 'flashcards', 'shortAnswer')
        topic: User-provided topic
        num_items: Number of questions/cards generated
        difficulty: Difficulty level ('beginner', 'intermediate', 'advanced')
        metadata: Additional type-specific details (numOptions, cardType, etc.)
        user_session_id: Optional user session UUID
    """
    try:
        # Use shared connection (RDS Proxy handles pooling)
        conn = get_db_connection()
        
        cursor = conn.cursor()
        
        # Insert analytics record
        cursor.execute(
            """
            INSERT INTO practice_material_analytics 
            (textbook_id, user_session_id, material_type, topic, num_items, difficulty, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                textbook_id,
                user_session_id,
                material_type,
                topic,
                num_items,
                difficulty,
                json.dumps(metadata)
            )
        )
        
        conn.commit()
        cursor.close()
        # Don't close connection - it's reused across invocations
        
        logger.info(f"Analytics tracked: {material_type} for textbook {textbook_id}")
        
    except Exception as e:
        logger.error(f"Failed to track analytics: {e}")
        # Don't fail the request if analytics tracking fails
        pass


def parse_body(body: str | None) -> Dict[str, Any]:
    if not body:
        return {}
    try:
        return json.loads(body)
    except Exception:
        return {}


def extract_json(text: str) -> Dict[str, Any]:
    s = text.find("{")
    e = text.rfind("}")
    if s == -1 or e == -1 or e <= s:
        raise ValueError("Model response did not contain JSON object")
    return json.loads(text[s : e + 1])





def handler(event, context):
    global _is_cold_start
    start_time = time.time()
    cold_start_duration_ms = None
    if _is_cold_start:
        cold_start_duration_ms = int((time.time() - start_time) * 1000)
        _is_cold_start = False

    def finalize(resp):
        execution_ms = int((time.time() - start_time) * 1000)
        emit_cold_start_metrics(context.function_name, execution_ms, cold_start_duration_ms)
        return resp

    logger.info("PracticeMaterial Lambda (Docker) invoked")

    # Initialize constants (models, guardrails) if not already loaded
    # This checks global variables, so it's fast if already initialized (e.g. by Provisioned Concurrency)
    try:
        initialize_constants()
    except Exception as e:
        logger.error(f"Failed to initialize constants: {e}")
        # Proceeding might fail later, but we log it.
        # Guardrails will be skipped if ID is missing (fail-open currently for config missing, but apply_guardrails handles None)

    # Handle warmup requests - return immediately after initialization
    if event.get("warmup") or event.get("httpMethod") == "HEAD":
        logger.info("Warmup request received - initializing and returning early")
        try:
            initialize_constants()
            logger.info("Warmup successful - models initialized")
        except Exception as e:
            logger.warning(f"Warmup initialization failed: {e}")
        return {"statusCode": 200, "body": json.dumps({"status": "warm"})}

    # Validate path and parse inputs
    resource = (event.get("httpMethod", "") + " " + event.get("resource", "")).strip()
    
    # Handle grading endpoint
    if resource == "POST /textbooks/{textbook_id}/practice_materials/grade":
        return finalize(handle_grading(event, context))
    
    # Handle generation endpoint
    if resource != "POST /textbooks/{textbook_id}/practice_materials":
        return finalize({"statusCode": 404, "body": json.dumps({"error": f"Unsupported route: {resource}"})})

    path_params = event.get("pathParameters") or {}
    textbook_id = path_params.get("textbook_id")
    if not textbook_id:
        return finalize({"statusCode": 400, "body": json.dumps({"error": "Textbook ID is required"})})

    body = parse_body(event.get("body"))
    topic = str(body.get("topic", "")).strip()
    if not topic:
        return finalize({"statusCode": 400, "body": json.dumps({"error": "'topic' is required"})})
    
    # Apply input guardrails on topic
    topic_guardrail_result = apply_guardrails(topic, source="INPUT")
    if topic_guardrail_result.get('blocked', False):
        logger.warning(f"SECURITY: Topic blocked by guardrails: {topic}")
        # Determine error message based on whether it was a technical error or content policy
        if topic_guardrail_result.get('error'):
            logger.error(f"SECURITY: Guardrail error: {topic_guardrail_result.get('error')}")
            error_message = "I'm experiencing technical difficulties and cannot process your request at this time. Please try again later."
        else:
            error_message = "I'm here to help with your learning! However, I can't generate practice materials for that particular topic. Let's focus on educational content instead."
        
        # Send error via WebSocket for streaming clients
        request_context = event.get("requestContext") or {}
        is_websocket = event.get("isWebSocket", False)
        if is_websocket:
            send_websocket_progress(
                request_context.get("connectionId"),
                request_context.get("domainName"),
                request_context.get("stage"),
                "error", 0,
                error=error_message
            )
        return finalize({"statusCode": 400, "body": json.dumps({
            "error": "Topic not allowed by content policy",
            "guardrail_blocked": True
        })})
    
    
    material_type = str(body.get("material_type", "mcq")).lower().strip()
    if material_type not in ["mcq", "flashcard", "short_answer"]:
        return finalize({"statusCode": 400, "body": json.dumps({"error": "material_type must be 'mcq', 'flashcard', or 'short_answer'"})})

    difficulty = str(body.get("difficulty", "intermediate")).lower().strip()
    
    # MCQ-specific parameters
    num_questions = clamp(int(body.get("num_questions", 5)), 1, 20)
    num_options = clamp(int(body.get("num_options", 4)), 2, 6)
    
    # Flashcard-specific parameters
    num_cards = clamp(int(body.get("num_cards", 10)), 1, 20)
    card_type = str(body.get("card_type", "definition")).lower().strip()
    
    # Short answer-specific parameters
    # For short answers, reuse num_questions but with different limits
    if material_type == "short_answer":
        num_questions = clamp(int(body.get("num_questions", 3)), 1, 10)

    # Force fresh generation (bypasses cache)
    force_fresh = body.get("force_fresh", False)
    if isinstance(force_fresh, str):
        force_fresh = force_fresh.lower() == "true"
    logger.info(f"force_fresh parameter: {force_fresh} (raw: {body.get('force_fresh', 'NOT_PRESENT')})")

    # Generate cache key based on request parameters
    num_items = num_cards if material_type == "flashcard" else num_questions
    extra_params = f"{num_options}" if material_type == "mcq" else card_type if material_type == "flashcard" else ""
    cache_key = generate_cache_key(
        textbook_id=textbook_id,
        topic=topic,
        material_type=material_type,
        difficulty=difficulty,
        num_items=num_items,
        extra_params=extra_params
    )
    
    # Check cache first (unless force_fresh is True)
    cached_response = None if force_fresh else get_cached_response(cache_key)
    if cached_response is not None:
        logger.info(f"Returning cached response for {material_type} on topic '{topic}'")
        response_data = {
            **cached_response["result"],
            "sources_used": cached_response["sources"],
            "cached": True  # Indicate this was a cached response
        }
        
        # Extract WebSocket context for streaming progress updates
        request_context = event.get("requestContext") or {}
        is_websocket = event.get("isWebSocket", False)
        connection_id = request_context.get("connectionId") if is_websocket else None
        domain_name = request_context.get("domainName") if is_websocket else None
        stage = request_context.get("stage") if is_websocket else None
        
        # Send immediate completion via WebSocket if applicable
        if is_websocket:
            send_websocket_progress(connection_id, domain_name, stage, "complete", 100, data=response_data)
            return finalize({"statusCode": 200})
        
        # For REST API, return full response
        return finalize({
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "*",
            },
            "body": json.dumps(response_data)
        })

    # Extract WebSocket context for streaming progress updates
    request_context = event.get("requestContext") or {}
    is_websocket = event.get("isWebSocket", False)
    connection_id = request_context.get("connectionId") if is_websocket else None
    domain_name = request_context.get("domainName") if is_websocket else None
    stage = request_context.get("stage") if is_websocket else None
    
    # Helper to send progress updates
    def send_progress(status: str, progress: int, data=None, error=None):
        send_websocket_progress(connection_id, domain_name, stage, status, progress, data, error)

    try:
        # Stage 1: Initialize
        send_progress("initializing", 5)
        logger.info("Initializing constants from SSM parameters...")
        initialize_constants()
        logger.info("Constants initialized successfully")

        # Stage 2: Get DB credentials
        send_progress("initializing", 10)
        logger.info("Getting DB credentials...")
        db = get_secret_dict(SM_DB_CREDENTIALS)
        logger.info("DB credentials retrieved")
        
        vectorstore_config = {
            "dbname": db["dbname"],
            "user": db["username"],
            "password": db["password"],
            "host": RDS_PROXY_ENDPOINT,
            "port": db["port"],
        }

        # Stage 3: Build retriever
        send_progress("retrieving", 15)
        logger.info(f"Building retriever for textbook {textbook_id}...")
        
        # Get connection for database operations (RDS Proxy handles pooling)
        conn = get_db_connection()
        
        retriever = get_textbook_retriever(
            llm=None,
            textbook_id=textbook_id,
            vectorstore_config_dict=vectorstore_config,
            embeddings=_embeddings,
            connection=conn,
        )
        logger.info("Retriever built successfully")
        send_progress("retrieving", 20)
        
        if retriever is None:
            return finalize({
                "statusCode": 404,
                "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"error": f"No embeddings found for textbook {textbook_id}"}),
            })

        # Stage 4: Invoke retriever
        send_progress("retrieving", 25)
        logger.info(f"Invoking retriever for topic: {topic}")
        docs = retriever.invoke(topic)
        logger.info(f"Retrieved {len(docs)} documents")
        send_progress("retrieving", 30)
        
        snippets = [d.page_content.strip()[:500] for d in docs][:6]
        
        # Extract sources from retrieved documents 
        sources_used = extract_sources_from_docs(docs)
        logger.info(f"Extracted {len(sources_used)} sources: {sources_used}")

        # Stage 5: Build prompt
        send_progress("generating", 35)
        logger.info(f"Building prompt for {material_type}...")
        if material_type == "mcq":
            prompt = build_mcq_prompt(topic, difficulty, num_questions, num_options, snippets)
        elif material_type == "flashcard":
            prompt = build_flashcard_prompt(topic, difficulty, num_cards, card_type, snippets)
        else:  # short_answer
            prompt = build_short_answer_prompt(topic, difficulty, num_questions, snippets)
        logger.info(f"Prompt built, length: {len(prompt)} chars")

        # Stage 6: Invoke LLM (the slowest part - ~15 seconds)
        send_progress("generating", 40)
        logger.info(f"Invoking LLM for {material_type} generation...")
        response = _llm.invoke(prompt)
        output_text = response.content
        logger.info(f"LLM response received, length: {len(output_text)} chars")
        send_progress("validating", 85)
        
        # Log raw output for debugging
        logger.info(f"Raw LLM output: {output_text}")

        # Parse and validate response
        logger.info("Parsing and validating LLM response...")
        try:
            if material_type == "mcq":
                result = validate_mcq_shape(extract_json(output_text), num_questions, num_options)
            elif material_type == "flashcard":
                result = validate_flashcard_shape(extract_json(output_text), num_cards)
            else:  # short_answer
                result = validate_short_answer_shape(extract_json(output_text), num_questions)
            logger.info("Validation successful")
        except Exception as e1:
            logger.warning(f"First parse/validation failed: {e1}")
            logger.warning(f"Raw LLM output (first 2000 chars): {output_text[:2000]}")
            retry_prompt = prompt + "\n\nIMPORTANT: Your previous response was invalid. You MUST return valid JSON only, exactly matching the schema and lengths. No extra commentary."
            logger.info("Retrying with enhanced prompt...")
            
            response2 = _llm.invoke(retry_prompt)
            output_text2 = response2.content
            logger.info(f"Retry response received, length: {len(output_text2)} chars")
            logger.info(f"Raw retry LLM output: {output_text2}")
            
            try:
                if material_type == "mcq":
                    result = validate_mcq_shape(extract_json(output_text2), num_questions, num_options)
                elif material_type == "flashcard":
                    result = validate_flashcard_shape(extract_json(output_text2), num_cards)
                else:  # short_answer
                    result = validate_short_answer_shape(extract_json(output_text2), num_questions)
                logger.info("Retry validation successful")
            except Exception as e2:
                logger.error(f"Retry also failed: {e2}")
                logger.error(f"Raw retry output (first 2000 chars): {output_text2[:2000]}")
                # Send error via WebSocket for streaming clients
                send_progress("error", 0, error=f"Failed to parse LLM response: {str(e2)}")
                # Return the raw LLM responses to client for debugging
                return finalize({
                    "statusCode": 500,
                    "headers": {
                        "Content-Type": "application/json",
                        "Access-Control-Allow-Headers": "*",
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Methods": "*",
                    },
                    "body": json.dumps({
                        "error": f"Failed to parse LLM response after retry: {str(e2)}",
                        "firstAttemptError": str(e1),
                        "rawFirstResponse": output_text,
                        "rawRetryResponse": output_text2,
                        "debug": "Check the raw responses above to see what the LLM generated"
                    })
                })

        # Apply output guardrails on generated content
        # Convert result to string for guardrail check
        result_text = json.dumps(result)
        output_guardrail_result = apply_guardrails(result_text, source="OUTPUT")
        if output_guardrail_result.get('blocked', False):
            # Determine error message based on whether it was a technical error or content policy
            if output_guardrail_result.get('error'):
                logger.error(f"SECURITY: Output guardrail error: {output_guardrail_result.get('error')}")
                error_message = "I apologize, but I'm experiencing technical difficulties. Please try again later."
            else:
                logger.warning("SECURITY: Generated content blocked by output guardrails")
                error_message = "The generated content was filtered by our safety policy. Please try a different topic."
            
            send_progress("error", 0, error=error_message)
            return finalize({"statusCode": 400, "body": json.dumps({
                "error": "Generated content blocked by content policy",
                "guardrail_blocked": True
            })})
        
        
        # Add sources to response 
        response_data = {
            **result,
            "sources_used": sources_used
        }
        
        # Store in cache for future requests
        set_cached_response(cache_key, result, sources_used)
        logger.info(f"Cached response for {material_type} on topic '{topic}'")
        
        # Track analytics (async, non-blocking)
        try:
            # Prepare metadata based on material type
            analytics_metadata = {}
            if material_type == "mcq":
                analytics_metadata = {
                    "numOptions": num_options,
                    "numQuestions": num_questions
                }
            elif material_type == "flashcard":
                analytics_metadata = {
                    "cardType": card_type,
                    "numCards": num_cards
                }
            else:  # short_answer
                analytics_metadata = {
                    "numQuestions": num_questions
                }
            
            # Get user_session_id from query parameters if available
            query_params = event.get("queryStringParameters") or {}
            user_session_id = query_params.get("user_session_id")
            
            # Determine num_items based on material type
            if material_type == "flashcard":
                num_items_generated = num_cards
            else:
                num_items_generated = num_questions
            
            # Track the generation
            track_practice_material_analytics(
                textbook_id=textbook_id,
                material_type=material_type,
                topic=topic,
                num_items=num_items_generated,
                difficulty=difficulty,
                metadata=analytics_metadata,
                user_session_id=user_session_id
            )
        except Exception as analytics_error:
            logger.warning(f"Analytics tracking failed but continuing: {analytics_error}")
        
        # Send completion via WebSocket if applicable
        send_progress("complete", 100, data=response_data)
        
        # For WebSocket invocations, return minimal response (data sent via WebSocket)
        if is_websocket:
            return {"statusCode": 200}
        
        # For REST API invocations, return full response
        return finalize({
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "*",
            },
            "body": json.dumps(response_data)
        })
    except Exception as e:
        logger.exception("Error generating practice materials")
        # Send error via WebSocket if applicable
        send_progress("error", 0, error=str(e))
        return finalize({"statusCode": 500, "body": json.dumps({"error": str(e)})})


def handle_grading(event, context):
    """
    Handle grading of a student's short answer response.
    """
    logger.info("Grading endpoint invoked")
    
    body = parse_body(event.get("body"))
    
    # Extract required fields
    question = str(body.get("question", "")).strip()
    student_answer = str(body.get("student_answer", "")).strip()
    sample_answer = str(body.get("sample_answer", "")).strip()
    key_points = body.get("key_points", [])
    rubric = str(body.get("rubric", "")).strip()
    
    # Validate inputs
    if not question:
        return {"statusCode": 400, "body": json.dumps({"error": "question is required"})}
    if not student_answer:
        return {"statusCode": 400, "body": json.dumps({"error": "student_answer is required"})}
    if not sample_answer:
        return {"statusCode": 400, "body": json.dumps({"error": "sample_answer is required"})}
    if not isinstance(key_points, list) or len(key_points) == 0:
        return {"statusCode": 400, "body": json.dumps({"error": "key_points must be a non-empty array"})}
    if not rubric:
        return {"statusCode": 400, "body": json.dumps({"error": "rubric is required"})}
    
    try:
        # Initialize constants if needed
        initialize_constants()
        
        # Build grading prompt
        prompt = build_grading_prompt(question, student_answer, sample_answer, key_points, rubric)
        
        # Get LLM response
        logger.info("Invoking LLM for grading")
        response = _llm.invoke(prompt)
        output_text = response.content
        logger.info(f"Received grading response from LLM, length: {len(output_text)}")
        logger.info(f"Raw grading output: {output_text}")
        
        # Parse JSON response
        try:
            result = extract_json(output_text)
            
            # Validate expected fields
            if not isinstance(result.get("feedback"), str):
                raise ValueError("feedback must be a string")
            if not isinstance(result.get("strengths"), list):
                raise ValueError("strengths must be an array")
            if not isinstance(result.get("improvements"), list):
                raise ValueError("improvements must be an array")
            if not isinstance(result.get("keyPointsCovered"), list):
                raise ValueError("keyPointsCovered must be an array")
            if not isinstance(result.get("keyPointsMissed"), list):
                raise ValueError("keyPointsMissed must be an array")
                
        except Exception as e1:
            logger.warning(f"First grading parse failed: {e1}")
            # Retry with enhanced prompt
            retry_prompt = prompt + "\n\nIMPORTANT: Your previous response was invalid. Return valid JSON only."
            logger.info("Retrying grading with enhanced prompt")
            response2 = _llm.invoke(retry_prompt)
            output_text2 = response2.content
            logger.info(f"Retry grading response: {output_text2}")
            
            try:
                result = extract_json(output_text2)
            except Exception as e2:
                logger.error(f"Retry grading also failed: {e2}")
                return {
                    "statusCode": 500,
                    "headers": {
                        "Content-Type": "application/json",
                        "Access-Control-Allow-Headers": "*",
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Methods": "*",
                    },
                    "body": json.dumps({
                        "error": f"Failed to parse grading response: {str(e2)}",
                        "rawResponse": output_text2
                    })
                }
        
        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "*",
            },
            "body": json.dumps(result),
        }
        
    except Exception as e:
        logger.exception("Error grading answer")
        return {
            "body": json.dumps({"error": f"Error grading answer: {str(e)}"}),
        }

# Global initialization for Provisioned Concurrency
# SSM parameters are pre-loaded at module import time (lines 48-73)
# This block initializes LLM and embeddings when the container is created
try:
    logger.info("Starting global LLM/embeddings initialization")
    initialize_constants()
    logger.info("Global initialization complete")
except Exception as e:
    # Log error but don't fail import - handler will retry
    logger.warning(f"Global initialization failed (will retry in handler): {e}")