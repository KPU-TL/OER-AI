import logging
import psycopg2
import traceback
from typing import Dict, Optional
from langchain_aws import BedrockEmbeddings
from .helper import get_vectorstore

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def get_vectorstore_retriever(llm, vectorstore_config_dict: Dict[str, str], embeddings):
    try:
        vectorstore, _ = get_vectorstore(
            collection_name=vectorstore_config_dict['collection_name'],
            embeddings=embeddings,
            dbname=vectorstore_config_dict['dbname'],
            user=vectorstore_config_dict['user'],
            password=vectorstore_config_dict['password'],
            host=vectorstore_config_dict['host'],
            port=int(vectorstore_config_dict['port'])
        )

        if vectorstore is None:
            logger.error("Failed to initialize vectorstore")
            return None

        search_kwargs = {
            "k": 5,
            "score_threshold": 0.2,
        }
        retriever = vectorstore.as_retriever(
            search_type="similarity_score_threshold",
            search_kwargs=search_kwargs,
        )
        logger.info(
            f"Created retriever with threshold {search_kwargs['score_threshold']} and k={search_kwargs['k']}"
        )
        return retriever
    except Exception as e:
        logger.error(f"Error in get_vectorstore_retriever: {str(e)}")
        logger.error(traceback.format_exc())
        return None


def get_textbook_retriever(llm, textbook_id: str, vectorstore_config_dict: Dict[str, str], embeddings: BedrockEmbeddings, selected_documents=None, connection=None) -> Optional[object]:
    logger.info(f"Creating retriever for textbook ID: {textbook_id}")
    try:
        # If a connection is provided, use it directly (caller manages lifecycle).
        # Otherwise, create a temporary one and close it when done.
        owns_connection = connection is None
        try:
            if connection is None:
                logger.debug("Creating direct database connection (no connection provided)")
                connection = psycopg2.connect(
                    dbname=vectorstore_config_dict['dbname'],
                    user=vectorstore_config_dict['user'],
                    password=vectorstore_config_dict['password'],
                    host=vectorstore_config_dict['host'],
                    port=int(vectorstore_config_dict['port'])
                )

            with connection.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM langchain_pg_collection WHERE name = %s", (textbook_id,))
                collection_exists = cur.fetchone()[0] > 0
                if not collection_exists:
                    logger.warning(f"Collection for textbook {textbook_id} does not exist")
                    return None
                cur.execute(
                    """
                    SELECT COUNT(*) FROM langchain_pg_embedding 
                    WHERE collection_id = (SELECT uuid FROM langchain_pg_collection WHERE name = %s)
                    """,
                    (textbook_id,),
                )
                embedding_count = cur.fetchone()[0]
                if embedding_count == 0:
                    logger.warning(f"No embeddings found for textbook {textbook_id}")
                    return None
        finally:
            # Only close the connection if we created it ourselves
            if owns_connection and connection and not connection.closed:
                connection.close()

        vectorstore_config_dict['collection_name'] = textbook_id
        retriever = get_vectorstore_retriever(
            llm=llm,
            vectorstore_config_dict=vectorstore_config_dict,
            embeddings=embeddings,
        )
        if retriever is None:
            logger.error(f"Failed to create retriever for textbook: {textbook_id}")
            return None
        logger.info(f"Successfully created retriever for textbook: {textbook_id}")
        return retriever
    except Exception as e:
        logger.error(f"Error in get_textbook_retriever: {str(e)}")
        logger.error(traceback.format_exc())
        return None
