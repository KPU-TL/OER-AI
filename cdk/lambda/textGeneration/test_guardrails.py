import boto3
import json

bedrock_runtime = boto3.client('bedrock-runtime', region_name='us-east-1')
guardrail_id = '<YOUR-GUARDRAIL-ID>'

# Example input to trigger a PII or prompt injection
content = 'What is my credit card number 4111-1111-1111-1111?'

response = bedrock_runtime.apply_guardrail(
    guardrailIdentifier=guardrail_id,
    guardrailVersion='1',  # Use published version, not "DRAFT"
    source='INPUT',
    content=[ { 'text': { 'text': content } } ],
)

print(json.dumps(response, indent=2))
