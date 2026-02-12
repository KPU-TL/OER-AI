"""
TextGeneration Lambda - AI Response Generation Service

=== ARCHITECTURE OVERVIEW ===

This Lambda function generates AI responses for the GenAI educational application.
It handles streaming responses via WebSocket, FAQ caching, token limiting, and security.

=== COLD START OPTIMIZATION STRATEGY ===

Docker Lambda cold starts can take 2-5 seconds. We optimize this by:

1. PRE-LOADING at module level (lines 47-92):
   - boto3 clients (SSM, Secrets Manager) are initialized when container starts
   - SSM parameters are fetched and cached in global variables
   - This happens BEFORE the first request, during container initialization

2. FALLBACK lazy-loading functions (lines 100-140):
   - These functions (get_ssm_client, get_secrets_manager, etc.) exist as FALLBACKS
   - They only create clients if the pre-loading try block failed
   - This ensures the Lambda works even if pre-loading encounters an error
   - In normal operation, these functions just return the pre-loaded client

3. DB CONNECTION POOL pre-warming (line ~93):
   - Connection pool is created at container startup, not on first request
   - Eliminates ~200ms latency on first database operation

=== KEY COMPONENTS ===

- main.py: Request handling, orchestration
- helpers/chat.py: LLM interaction, RAG chain, streaming
- helpers/vectorstore.py: Vector similarity search
- helpers/faq_cache.py: Semantic caching for frequent questions
- helpers/token_limit_helper.py: Daily usage limits
- helpers/session_security.py: Input validation and sanitization

# helpers/session_security.py: Input validation and sanitization
"""

import os
import json
import time
import logging
import threading

# Import custom exceptions
try:
    from helpers.exceptions import (
        TextGenerationError,
        ValidationError,
        ConfigurationError,
        TokenLimitError,
        UpstreamServiceError
    )
except ImportError:
    # Fallback for local testing if helpers path issues arise
    class TextGenerationError(Exception):
        def __init__(self, message, status_code=500, error_code="INTERNAL", details=None):
            self.status_code = status_code
            self.error_code = error_code
            self.message = message
            self.details = details
    
    class ValidationError(TextGenerationError):
        def __init__(self, m, d=None): super().__init__(m, 400, "VALIDATION", d)
    class ConfigurationError(TextGenerationError):
        def __init__(self, m): super().__init__(m, 500, "CONFIG")
    class TokenLimitError(TextGenerationError):
        def __init__(self, m, u=None): super().__init__(m, 429, "LIMIT", {"usage": u})
    class UpstreamServiceError(TextGenerationError):
        def __init__(self, m, s): super().__init__(f"{s}: {m}", 502, "UPSTREAM")

# Set up basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# =============================================================================

# Environment variables
DB_SECRET_NAME = os.environ["SM_DB_CREDENTIALS"]
REGION = os.environ["REGION"]
RDS_PROXY_ENDPOINT = os.environ["RDS_PROXY_ENDPOINT"]
BEDROCK_LLM_PARAM = os.environ.get("BEDROCK_LLM_PARAM")
EMBEDDING_MODEL_PARAM = os.environ.get("EMBEDDING_MODEL_PARAM")
BEDROCK_REGION_PARAM = os.environ.get("BEDROCK_REGION_PARAM")
GUARDRAIL_ID_PARAM = os.environ.get("GUARDRAIL_ID_PARAM")
EMBEDDING_REGION_PARAM = os.environ.get("EMBEDDING_REGION_PARAM")
WEBSOCKET_API_ENDPOINT = os.environ.get("WEBSOCKET_API_ENDPOINT", "")
TABLE_NAME_PARAM = os.environ.get("TABLE_NAME_PARAM")
DAILY_TOKEN_LIMIT_PARAM = os.environ.get("DAILY_TOKEN_LIMIT_PARAM")
COLD_START_METRIC = os.environ.get("COLD_START_METRIC", "false").lower() == "true"
FORCE_COLD_START_TEST = os.environ.get("FORCE_COLD_START_TEST", "false").lower() == "true"
# =============================================================================
# GLOBAL STATE - Pre-loaded at container startup for cold start optimization
# =============================================================================
# These variables are initialized in the try block below (lines ~116-178).
# The lazy-loading functions (get_ssm_client, etc.) are FALLBACKS that only
# create clients if pre-loading failed. In normal operation, they just return
# the pre-loaded client.

_secrets_manager = None  # Pre-loaded in try block below
_ssm_client = None       # Pre-loaded in try block below
_bedrock_runtime = None  # Lazy-loaded on first use (region may differ)
_db_connection = None    # Pre-warmed single connection (RDS Proxy handles pooling)
_db_secret = None        # Cached after first fetch
_embeddings = None       # Cached after first use
_is_cold_start = True    # Tracks cold start for metrics
_startup_ts = time.time()

# Pre-loaded configuration - loaded at container startup
BEDROCK_LLM_ID = None
EMBEDDING_MODEL_ID = None
BEDROCK_REGION = None
EMBEDDING_REGION = None
GUARDRAIL_ID = None

# Pre-load critical configuration during container startup (outside handler)
try:
    logger.info("Pre-loading critical configuration...")
    import boto3
    
    _ssm_client = boto3.client("ssm", region_name=REGION)
    _secrets_manager = boto3.client("secretsmanager", region_name=REGION)
    
    # Pre-fetch SSM parameters
    if BEDROCK_LLM_PARAM:
        BEDROCK_LLM_ID = _ssm_client.get_parameter(Name=BEDROCK_LLM_PARAM, WithDecryption=True)["Parameter"]["Value"]
        logger.info(f"Pre-loaded BEDROCK_LLM_ID: {BEDROCK_LLM_ID}")
    
    if EMBEDDING_MODEL_PARAM:
        EMBEDDING_MODEL_ID = _ssm_client.get_parameter(Name=EMBEDDING_MODEL_PARAM, WithDecryption=True)["Parameter"]["Value"]
        logger.info(f"Pre-loaded EMBEDDING_MODEL_ID: {EMBEDDING_MODEL_ID}")
    
    if BEDROCK_REGION_PARAM:
        BEDROCK_REGION = _ssm_client.get_parameter(Name=BEDROCK_REGION_PARAM, WithDecryption=True)["Parameter"]["Value"]
        logger.info(f"Pre-loaded BEDROCK_REGION: {BEDROCK_REGION}")
    else:
        BEDROCK_REGION = REGION
        logger.info(f"Using deployment region as BEDROCK_REGION: {BEDROCK_REGION}")
    
    if GUARDRAIL_ID_PARAM:
        GUARDRAIL_ID = _ssm_client.get_parameter(Name=GUARDRAIL_ID_PARAM, WithDecryption=True)["Parameter"]["Value"]
        logger.info(f"Pre-loaded GUARDRAIL_ID")
    
    if EMBEDDING_REGION_PARAM:
        EMBEDDING_REGION = _ssm_client.get_parameter(Name=EMBEDDING_REGION_PARAM, WithDecryption=True)["Parameter"]["Value"]
        logger.info(f"Pre-loaded EMBEDDING_REGION: {EMBEDDING_REGION}")
    else:
        EMBEDDING_REGION = "us-east-1"  # Default for Cohere Embed v4
        logger.info(f"Using default EMBEDDING_REGION: {EMBEDDING_REGION}")
    
    logger.info(f"Pre-loading completed in {time.time() - _startup_ts:.2f}s")
    
    # PRE-WARM DATABASE CONNECTION
    # Creating a connection at startup eliminates ~200ms latency on first DB operation.
    # RDS Proxy handles connection pooling, so we only need one connection per Lambda container.
    try:
        logger.info("Pre-warming database connection...")
        import psycopg2
        db_secret_response = _secrets_manager.get_secret_value(SecretId=DB_SECRET_NAME)
        db_creds = json.loads(db_secret_response["SecretString"])
        _db_secret = db_creds  # Cache the secret
        _db_connection = psycopg2.connect(
            host=RDS_PROXY_ENDPOINT,
            database=db_creds["dbname"],
            user=db_creds["username"],
            password=db_creds["password"],
            port=int(db_creds["port"])
        )
        logger.info("Database connection pre-warmed successfully")
    except Exception as conn_error:
        logger.warning(f"Failed to pre-warm connection (will create on-demand): {conn_error}")
        
except Exception as e:
    logger.warning(f"Pre-loading failed (will load on-demand via fallback functions): {e}")


# =============================================================================
# FALLBACK FUNCTIONS - Only execute if pre-loading failed
# =============================================================================
# These functions implement the "lazy loading" pattern as a FALLBACK mechanism.
# In normal operation (when pre-loading succeeds), these functions simply return
# the already-initialized global client. They only create new clients if the
# global is None (meaning pre-loading failed for some reason).
#
# This is DEFENSIVE PROGRAMMING, not duplicate initialization.
# =============================================================================

def get_secrets_manager():
    """Return the pre-loaded Secrets Manager client. Fails fast if not initialized."""
    if _secrets_manager is None:
        raise ConfigurationError("Secrets Manager client not initialized. Pre-loading failed.")
    return _secrets_manager


def get_ssm_client():
    """Return the pre-loaded SSM client. Fails fast if not initialized."""
    if _ssm_client is None:
        raise ConfigurationError("SSM client not initialized. Pre-loading failed.")
    return _ssm_client


def get_bedrock_runtime():
    """
    Get Bedrock runtime client for embeddings.
    
    NOTE: This is intentionally lazy-loaded (not pre-loaded) because:
    - Bedrock region may be different from the Lambda's region
    - The region is determined by EMBEDDING_REGION which is fetched from SSM
    - We need SSM parameters loaded first before knowing which region to use
    """
    global _bedrock_runtime
    if _bedrock_runtime is None:
        import boto3
        # Use EMBEDDING_REGION from SSM parameter (defaults to us-east-1 for Cohere Embed v4)
        embedding_region = EMBEDDING_REGION or "us-east-1"
        _bedrock_runtime = boto3.client("bedrock-runtime", region_name=embedding_region)
        logger.info(f"Bedrock runtime client initialized for region: {embedding_region}")
    return _bedrock_runtime


def get_embeddings():
    """
    Get embeddings model instance (cached after first initialization).
    
    NOTE: Embeddings are cached globally to avoid re-initialization on each request.
    The BedrockEmbeddings object is stateless and thread-safe, so sharing is fine.
    """
    global _embeddings
    if _embeddings is None:
        from langchain_aws import BedrockEmbeddings
        bedrock_runtime = get_bedrock_runtime()
        embedding_region = EMBEDDING_REGION or "us-east-1"
        _embeddings = BedrockEmbeddings(
            model_id=EMBEDDING_MODEL_ID,
            client=bedrock_runtime,
            region_name=embedding_region,
            model_kwargs={"input_type": "search_document"}
        )
        logger.info(f"Initialized embeddings with model: {EMBEDDING_MODEL_ID} in region: {embedding_region}")
    return _embeddings


def emit_cold_start_metrics(function_name: str, execution_ms: int, cold_start_ms: int | None) -> None:
    """Emit embedded CloudWatch metrics for cold start and execution time."""
    if not COLD_START_METRIC:
        return

    metrics_payload = {
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

    print(json.dumps(metrics_payload))


def get_secret(secret_name, expect_json=True):
    """Get secret from Secrets Manager with caching"""
    global _db_secret
    if _db_secret is None:
        try:
            secrets_manager = get_secrets_manager()
            response = secrets_manager.get_secret_value(SecretId=secret_name)["SecretString"]
            _db_secret = json.loads(response) if expect_json else response
        except Exception as e:
            logger.error(f"Error fetching secret: {e}")
            raise
    return _db_secret


def get_parameter(param_name, cached_var):
    """Get parameter from SSM Parameter Store"""
    if cached_var is None and param_name:
        try:
            ssm = get_ssm_client()
            response = ssm.get_parameter(Name=param_name, WithDecryption=True)
            cached_var = response["Parameter"]["Value"]
        except Exception as e:
            logger.error(f"Error fetching parameter {param_name}: {e}")
            raise
    return cached_var


def initialize_constants():
    """Initialize constants - now mostly a no-op since we pre-load at startup"""
    # Constants are already pre-loaded at module level
    # This function is kept for compatibility but does nothing now
    if BEDROCK_LLM_ID is None:
        logger.warning("BEDROCK_LLM_ID not pre-loaded, this should not happen")
    pass


def get_db_connection():
    """
    Get or create a database connection (RDS Proxy handles pooling).
    
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
    import psycopg2
    for attempt in range(2):
        logger.info(f"Creating new database connection (attempt {attempt + 1}/2)")
        try:
            secret = get_secret(DB_SECRET_NAME)
            _db_connection = psycopg2.connect(
                host=RDS_PROXY_ENDPOINT,
                database=secret["dbname"],
                user=secret["username"],
                password=secret["password"],
                port=int(secret["port"])
            )
            logger.info("Database connection created")
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


def get_db_credentials():
    """Get database credentials from Secrets Manager"""
    try:
        return get_secret(DB_SECRET_NAME)
    except Exception as e:
        logger.error(f"Error fetching DB credentials: {e}")
        raise


def connect_to_db():
    """Get the database connection (alias for get_db_connection for compatibility)."""
    return get_db_connection()


def return_db_connection(connection):
    """No-op: Connection is reused, not returned to pool (RDS Proxy handles pooling)."""
    # Do nothing - we reuse the same connection
    # RDS Proxy manages the actual connection pool
    pass


def estimate_token_count(text: str) -> int:
    """
    Estimate the number of tokens in a text string.
    Uses a simple word-based approximation: ~1.3 tokens per word for English text.
    
    Args:
        text: The text to estimate tokens for
    
    Returns:
        Estimated token count
    """
    if not text:
        return 0
    # Simple approximation: split by whitespace and multiply by 1.3
    word_count = len(text.split())
    return int(word_count * 1.3)


# This function is now a wrapper for the helper function in chat.py
def process_query_streaming(query, textbook_id, retriever, chat_session_id, websocket_endpoint, connection_id, connection=None):
    """
    Process a query using streaming response via WebSocket
    """
    # Lazy import
    from helpers.chat import get_bedrock_llm, get_response_streaming
    
    logger.info(f"Processing streaming query with LLM model ID: '{BEDROCK_LLM_ID}'")
    
    try:
        # Initialize LLM
        logger.info(f"Initializing Bedrock LLM with model ID: {BEDROCK_LLM_ID}")
        llm = get_bedrock_llm(BEDROCK_LLM_ID, bedrock_region=BEDROCK_REGION)
        
        # Use the streaming helper function from chat.py
        logger.info(f"Calling get_response_streaming with textbook_id: {textbook_id}")
        return get_response_streaming(
            query=query,
            textbook_id=textbook_id,
            llm=llm,
            retriever=retriever,
            chat_session_id=chat_session_id,
            connection=connection,
            guardrail_id=GUARDRAIL_ID,
            websocket_endpoint=websocket_endpoint,
            connection_id=connection_id,
            table_name=TABLE_NAME_PARAM,
            bedrock_llm_id=BEDROCK_LLM_ID
        )
    except Exception as e:
        logger.error(f"Error in process_query_streaming: {str(e)}", exc_info=True)
        # Send error message via WebSocket
        try:
            import boto3
            apigatewaymanagementapi = boto3.client('apigatewaymanagementapi', endpoint_url=websocket_endpoint)
            apigatewaymanagementapi.post_to_connection(
                ConnectionId=connection_id,
                Data=json.dumps({
                    "type": "error",
                    "message": "I'm sorry, I encountered an error while processing your question."
                })
            )
        except Exception as ws_error:
            logger.error(f"Failed to send the error via WebSocket: {ws_error}")
        
        return {
            "response": f"I'm sorry, I encountered an error while processing your question.",
            "sources_used": []
        }


def process_query(query, textbook_id, retriever, chat_session_id, connection=None):
    """
    Process a query using the chat helper function
    
    Args:
        query: The user's question
        textbook_id: ID of the textbook
        retriever: Vector store retriever for the textbook
        connection: Optional database connection for fetching custom prompts
        
    Returns:
        Response dictionary with answer and sources used
    """
    # Lazy import
    from helpers.chat import get_bedrock_llm, get_response
    
    # Log the model ID being used
    logger.info(f"Processing query with LLM model ID: '{BEDROCK_LLM_ID}'")
    logger.info(f"Environment variables: REGION={REGION}, RDS_PROXY_ENDPOINT={RDS_PROXY_ENDPOINT}")
    
    try:
        # Initialize LLM
        logger.info(f"Initializing Bedrock LLM with model ID: {BEDROCK_LLM_ID}")
        llm = get_bedrock_llm(BEDROCK_LLM_ID, bedrock_region=BEDROCK_REGION)
        
        # Test the LLM with a simple message to verify it works
        try:
            logger.info(f"Testing LLM with a simple message...")
            test_message = llm.invoke("This is a test. Respond with 'OK' if you receive this message.")
            logger.info(f"LLM test successful. Response content type: {type(test_message)}")
        except Exception as test_error:
            logger.error(f"LLM test failed: {str(test_error)}")
            logger.error(f"This may indicate the model ID {BEDROCK_LLM_ID} is not available in region {REGION}")
            raise
        
        # Log the embeddings model ID for context
        logger.info(f"Using embedding model ID: {EMBEDDING_MODEL_ID}")
        
        # Use the helper function from chat.py to generate the response
        logger.info(f"Calling get_response with textbook_id: {textbook_id}")
        return get_response(
            query=query,
            textbook_id=textbook_id,
            llm=llm,
            retriever=retriever,
            chat_session_id=chat_session_id,
            connection=connection,
            guardrail_id=GUARDRAIL_ID
        )
    except Exception as e:
        logger.error(f"Error in process_query: {str(e)}", exc_info=True)
        logger.error(f"Model ID: {BEDROCK_LLM_ID}, Region: {REGION}")
        # Return a graceful error message
        return {
            "response": f"I'm sorry, I encountered an error while processing your question. The error has been logged for our team to investigate.",
            "sources_used": []
        }

# =============================================================================
# REFACTORED HELPER FUNCTIONS
# =============================================================================

def parse_and_validate_request(event):
    """
    Extract and validate parameters from the Lambda event.
    
    Args:
        event: The Lambda event object
        
    Returns:
        tuple: (question, textbook_id, chat_session_id, is_websocket, connection_id, websocket_endpoint)
        
    Raises:
        ValidationError: If required parameters are missing or invalid
    """
    # Check for WebSocket invocation
    connection_id = event.get("requestContext", {}).get("connectionId")
    is_websocket = connection_id is not None
    
    domain_name = event.get("requestContext", {}).get("domainName")
    stage = event.get("requestContext", {}).get("stage")
    websocket_endpoint = f"https://{domain_name}/{stage}" if domain_name and stage else ""
    
    # Extract path parameters
    path_params = event.get("pathParameters", {}) or {}
    chat_session_id = path_params.get("id", "")
    
    # Parse body
    body = {}
    if event.get("body"):
        try:
            body = json.loads(event.get("body"))
        except json.JSONDecodeError:
            raise ValidationError("Invalid JSON body")
        
    question = body.get("query", "")
    textbook_id = body.get("textbook_id", "")
    
    # Validation
    if not textbook_id:
        raise ValidationError("Missing textbook_id parameter")
    
    if not question:
        raise ValidationError("No question provided in the query field")
        
    return question, textbook_id, chat_session_id, is_websocket, connection_id, websocket_endpoint


def enforce_token_limits(connection, chat_session_id, ssm_client, is_websocket, connection_id, websocket_endpoint):
    """
    Check if the user has exceeded their daily token limit.
    
    Returns:
        bool: True if check passed (or unlimited)
        
    Raises:
        TokenLimitError: If limit is exceeded
    """
    # Lazy import
    from helpers.token_limit_helper import get_user_session_from_chat_session, get_session_token_status
    
    if not chat_session_id or not DAILY_TOKEN_LIMIT_PARAM:
        return True
        
    try:
        # Get user_session_id
        user_session_id = get_user_session_from_chat_session(connection, chat_session_id)
        
        if not user_session_id:
            return True
            
        # Check status
        token_status = get_session_token_status(
            connection=connection,
            user_session_id=user_session_id,
            global_limit_param_name=DAILY_TOKEN_LIMIT_PARAM,
            ssm_client=ssm_client
        )
        
        daily_limit = token_status.get('daily_limit')
        remaining_tokens = token_status.get('remaining_tokens', 0)
        
        # If limit exceeded
        if daily_limit != float('inf') and remaining_tokens <= 0:
            hours_until_reset = token_status.get('hours_until_reset', 0)
            reset_time = token_status.get('reset_time', '')
            tokens_used = token_status.get('tokens_used', 0)
            
            error_message = f"You have reached your daily token limit of {daily_limit:,} tokens. Your limit will reset in {hours_until_reset:.1f} hours."
            logger.warning(f"Token limit exceeded for user_session {user_session_id}: {tokens_used}/{daily_limit}")
            
            # Send WebSocket error if applicable
            if is_websocket and connection_id and websocket_endpoint:
                try:
                    import boto3
                    apigatewaymanagementapi = boto3.client('apigatewaymanagementapi', endpoint_url=websocket_endpoint)
                    apigatewaymanagementapi.post_to_connection(
                        ConnectionId=connection_id,
                        Data=json.dumps({
                            "type": "error",
                            "message": error_message,
                            "error_code": "TOKEN_LIMIT_EXCEEDED"
                        })
                    )
                except Exception as ws_error:
                    logger.error(f"Failed to send token limit error via WebSocket: {ws_error}")
            
            # Raise exception to stop processing
            usage_info = {
                "tokens_used": tokens_used,
                "daily_limit": daily_limit,
                "remaining_tokens": 0,
                "hours_until_reset": hours_until_reset,
                "reset_time": reset_time
            }
            raise TokenLimitError(error_message, usage_info=usage_info)
            
        return True
        
    except TokenLimitError:
        raise
    except Exception as e:
        logger.error(f"Error in token pre-check: {e}", exc_info=True)
        # Fail open
        return True



def handle_faq_check(question, textbook_id, embeddings, connection, is_websocket, connection_id, websocket_endpoint):
    """
    Check FAQ cache and stream response if found (WebSocket only).
    """
    # Lazy import
    from helpers.faq_cache import check_faq_cache, stream_cached_response
    
    if not is_websocket:
        return None
        
    logger.info("Checking FAQ cache for similar questions...")
    cached_response = check_faq_cache(
        question=question,
        textbook_id=textbook_id,
        embeddings=embeddings,
        connection=connection
    )
    
    if cached_response:
        logger.info(f"Found cached response (similarity: {cached_response.get('similarity', 0):.4f})")
        stream_cached_response(
            cached_faq=cached_response,
            websocket_endpoint=websocket_endpoint,
            connection_id=connection_id
        )
        return cached_response
        
    return None


def generate_and_cache_response(question, textbook_id, retriever, connection, chat_session_id, is_websocket, connection_id, websocket_endpoint, embeddings):
    """
    Generate response using LLM and cache to FAQ if appropriate.
    """
    # Lazy import
    from helpers.faq_cache import cache_faq
    
    response_data = None
    
    if is_websocket:
        response_data = process_query_streaming(
            query=question,
            textbook_id=textbook_id,
            retriever=retriever,
            connection=connection,
            chat_session_id=chat_session_id,
            websocket_endpoint=websocket_endpoint,
            connection_id=connection_id
        )
        
        # Cache logic
        should_cache = (
            response_data.get("response") and
            not response_data.get("guardrail_blocked", False) and
            len(response_data.get("response", "")) > 50 and
            len(response_data.get("sources_used", [])) > 0
        )
        
        if should_cache:
            logger.info("Caching FAQ response for future use...")
            cache_metadata = {"sources_count": len(response_data.get("sources_used", []))}
            cache_faq(
                question=question,
                answer=response_data["response"],
                textbook_id=textbook_id,
                embeddings=embeddings,
                connection=connection,
                sources=response_data.get("sources_used", []),
                metadata=cache_metadata
            )
    else:
        logger.warning("Non-WebSocket API call detected - this is deprecated")
        response_data = process_query(
            query=question,
            textbook_id=textbook_id,
            retriever=retriever,
            connection=connection,
            chat_session_id=chat_session_id
        )
        
    return response_data


def track_usage_and_logs(connection, chat_session_id, question, response_data, textbook_id, is_websocket):
    """
    Handle post-response token usage tracking and async analytics logging.
    """
    # Lazy imports
    from helpers.token_limit_helper import get_user_session_from_chat_session, check_and_update_token_limit
    from helpers.chat import update_session_name
    
    # 1. Token Tracking
    if chat_session_id and DAILY_TOKEN_LIMIT_PARAM:
        try:
            user_session_id = get_user_session_from_chat_session(connection, chat_session_id)
            if user_session_id:
                # Calculate tokens
                token_usage = response_data.get('token_usage')
                if token_usage:
                    tokens_used = token_usage.get('total_tokens', 0)
                else:
                    input_tokens = estimate_token_count(question)
                    output_tokens = estimate_token_count(response_data.get('response', ''))
                    tokens_used = input_tokens + output_tokens
                
                # Update DB
                can_proceed, usage_info = check_and_update_token_limit(
                    connection=connection,
                    user_session_id=user_session_id,
                    tokens_to_add=tokens_used,
                    global_limit_param_name=DAILY_TOKEN_LIMIT_PARAM,
                    ssm_client=get_ssm_client()
                )
                
                if can_proceed:
                     logger.info(f"Token usage tracked. Total: {usage_info.get('tokens_used')}/{usage_info.get('daily_limit')}")
        except Exception as e:
            logger.error(f"Error tracking token usage: {e}", exc_info=True)

    # 2. Session Name Update (Sync)
    session_name = None
    if chat_session_id and TABLE_NAME_PARAM and not is_websocket:
        try:
            session_name = update_session_name(
                table_name=TABLE_NAME_PARAM,
                session_id=chat_session_id,
                bedrock_llm_id=BEDROCK_LLM_ID,
                db_connection=connection
            )
        except Exception as e:
            logger.error(f"Error updating session name: {e}")

    # 3. Async Logging
    if chat_session_id:
        def log_interaction_async(session_id, q, resp, sources, tb_id):
            async_conn = None
            try:
                async_conn = connect_to_db()
                with async_conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO user_interactions
                        (chat_session_id, sender_role, query_text, response_text, source_chunks)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (session_id, "User", q, resp, json.dumps(sources))
                    )
                async_conn.commit()
                logger.info(f"[ASYNC] Logged interaction for textbook {tb_id}")
            except Exception as async_error:
                logger.error(f"[ASYNC] Error logging interaction: {async_error}")
                if async_conn:
                    try:
                        async_conn.rollback()
                    except Exception as rollback_error:
                        logger.error(f"[ASYNC] Error during rollback: {rollback_error}", exc_info=True)
            finally:
                if async_conn:
                    return_db_connection(async_conn)
                    logger.debug("[ASYNC] Returned connection to pool")

        log_thread = threading.Thread(
            target=log_interaction_async,
            args=(chat_session_id, question, response_data["response"], 
                  response_data["sources_used"], textbook_id),
            daemon=False
        )
        log_thread.start()
        logger.info("Started async analytics logging thread")

    return session_name


# =============================================================================
# HANDLER HELPER FUNCTIONS
# =============================================================================

def _handle_get_request(event, finalize):
    """
    Handle GET requests for chat history retrieval.
    
    Args:
        event: Lambda event
        finalize: Callback to wrap response with metrics
        
    Returns:
        Lambda response dict
    """
    logger.info("Processing GET request for chat history")
    chat_session_id = event.get("pathParameters", {}).get("id")
    
    if not chat_session_id:
        return finalize({
            "statusCode": 400,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "*"
            },
            "body": json.dumps({"error": "Missing session ID"})
        })
    
    from helpers.chat import get_chat_history
    history = get_chat_history(chat_session_id)
    
    return finalize({
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*"
        },
        "body": json.dumps(history)
    })


def _setup_resources(textbook_id):
    """
    Initialize database connection, embeddings, and retriever for a textbook.
    
    Args:
        textbook_id: The textbook to set up resources for
        
    Returns:
        tuple: (connection, embeddings, retriever)
        
    Raises:
        UpstreamServiceError: If DB connection fails
        ValidationError: If no embeddings found for textbook
    """
    try:
        connection = connect_to_db()
    except Exception as e:
        raise UpstreamServiceError(f"Failed to connect to database: {str(e)}", "Database")
    
    embeddings = get_embeddings()
    
    from helpers.vectorstore import get_textbook_retriever
    db_creds = get_db_credentials()
    vectorstore_config = {
        "dbname": db_creds["dbname"],
        "user": db_creds["username"],
        "password": db_creds["password"],
        "host": RDS_PROXY_ENDPOINT,
        "port": db_creds["port"]
    }
    
    try:
        retriever = get_textbook_retriever(
            llm=None,
            textbook_id=textbook_id,
            vectorstore_config_dict=vectorstore_config,
            embeddings=embeddings
        )
        if retriever is None:
            raise ValidationError(f"No embeddings found for textbook {textbook_id}")
    except ValidationError:
        raise
    except Exception as re:
        raise UpstreamServiceError(f"Failed to initialize retriever: {str(re)}", "VectorStore")
    
    return connection, embeddings, retriever


def handler(event, context):
    """
    Lambda handler function for textbook question answering API endpoint.
    Refactored to reduce complexity and delegate responsibilities to helper functions.
    """
    global _is_cold_start
    start_time = time.time()
    cold_start_duration_ms = None
    
    # Cold Start Logic
    if FORCE_COLD_START_TEST:
        _is_cold_start = True
    if _is_cold_start:
        baseline = _startup_ts if not FORCE_COLD_START_TEST else start_time
        cold_start_duration_ms = int((time.time() - baseline) * 1000)
        logger.info(f"⚡ COLD START detected: {cold_start_duration_ms}ms since container start")
        _is_cold_start = False
    else:
        logger.info("♻️ WARM START")

    def finalize(resp):
        execution_ms = int((time.time() - start_time) * 1000)
        emit_cold_start_metrics(context.function_name, execution_ms, cold_start_duration_ms)
        logger.info(f"Total execution time: {execution_ms}ms")
        return resp

    logger.info("Starting textbook question answering Lambda")
    
    connection = None
    
    # Handle warmup request - initialize resources but return immediately
    if event.get("warmup"):
        logger.info("🔥 WARMUP request received")
        try:
            initialize_constants()
            _ = get_embeddings()  # Pre-load embeddings model
            connection = connect_to_db()
            return_db_connection(connection)
            warmup_duration_ms = int((time.time() - start_time) * 1000)
            logger.info(f"✅ WARMUP complete in {warmup_duration_ms}ms - container is warm")
        except Exception as e:
            logger.warning(f"Warmup encountered error (non-fatal): {e}")
        
        return {
            "statusCode": 200,
            "body": json.dumps({"warmup": "success"})
        }

    try:
        # 1. Initialization
        try:
            initialize_constants()
        except Exception as e:
            logger.error(f"❌ Failed to initialize constants: {e}")
            raise ConfigurationError(f"Configuration error: {str(e)}")

        # 2. Route by HTTP Method
        http_method = event.get("httpMethod", "")
        if http_method == "GET":
            return _handle_get_request(event, finalize)

        # POST Request Logic (Generation)
        question, textbook_id, chat_session_id, is_websocket, connection_id, websocket_endpoint = parse_and_validate_request(event)

        # 3. Security: Sanitize Session ID
        if chat_session_id:
            from helpers.session_security import sanitize_session_id
            try:
                chat_session_id = sanitize_session_id(chat_session_id)
            except ValueError as e:
                raise ValidationError("Invalid session ID format", {"original_error": str(e)})

        # 4. Resource Setup (DB & Retriever)
        connection, embeddings, retriever = _setup_resources(textbook_id)
        
        # 5. Token Check
        ssm_client = get_ssm_client()
        enforce_token_limits(connection, chat_session_id, ssm_client, is_websocket, connection_id, websocket_endpoint)

        # 5. Business Logic: FAQ Check OR Generate Response
        response_data = None
        from_cache = False
        
        # FAQ Check
        cached_response = handle_faq_check(question, textbook_id, embeddings, connection, is_websocket, connection_id, websocket_endpoint)
        
        if cached_response:
            response_data = {"response": cached_response["answer"], "sources_used": cached_response.get("sources", []), "cache_similarity": cached_response.get("similarity")}
            from_cache = True
        else:
            # Generate Response
            try:
                response_data = generate_and_cache_response(
                    question, textbook_id, retriever, connection, chat_session_id, 
                    is_websocket, connection_id, websocket_endpoint, embeddings
                )
            except Exception as query_error:
                logger.error(f"Error processing query: {query_error}", exc_info=True)
                raise UpstreamServiceError(f"Error processing query: {str(query_error)}", "LLM/Bedrock")

        # 6. Post-Processing (Usage Tracking & Logging)
        session_name = None
        if not from_cache:
            session_name = track_usage_and_logs(connection, chat_session_id, question, response_data, textbook_id, is_websocket)

        # 7. Final Response
        response_body = {
            "textbook_id": textbook_id,
            "response": response_data["response"],
            "sources": response_data.get("sources_used", []),
            "session_name": session_name if not is_websocket else response_data.get("session_name")
        }
        
        if from_cache or response_data.get("from_cache"):
            response_body["from_cache"] = True
            if "cache_similarity" in response_data:
                response_body["cache_similarity"] = response_data["cache_similarity"]
        
        return finalize({
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Allow-Origin": "*", 
                "Access-Control-Allow-Methods": "*"
            },
            "body": json.dumps(response_body)
        })

    except TextGenerationError as tge:
        logger.error(f"Request failed with {tge.error_code}: {tge.message}")
        if tge.details:
            logger.error(f"Error details: {tge.details}")
            
        error_body = {
            "error": tge.message,
            "code": tge.error_code
        }
        if tge.details:
            error_body["details"] = tge.details
            
        return finalize({
            "statusCode": tge.status_code,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS"
            },
            "body": json.dumps(error_body)
        })

        logger.error(f"Unhandled exception: {e}", exc_info=True)
        return finalize({
            "statusCode": 500,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS"
            },
            "body": json.dumps({"error": "Internal server error", "message": str(e)})
        })
        
    finally:
        # Ensure connection returned to pool
        if connection:
            return_db_connection(connection)