# Bedrock Guardrails Implementation

This document explains the Bedrock Guardrails implementation integrated into the OER-AI project. It highlights how the guardrails are created, configured, and enforced in the CDK and runtime lambda code (text generation), and provides tips for testing, monitoring, and troubleshooting.

## Introduction

### What is Amazon Bedrock?
Amazon Bedrock is a fully managed service that provides access to foundation models (LLMs) from leading AI providers through a single API. In this project, Amazon Bedrock powers the conversational AI assistant that helps students interact with textbook content.
**Learn more:** https://docs.aws.amazon.com/bedrock/

### How this project uses LLMs
The OER-AI Assistant uses Bedrock foundation models to:
- Answer student questions about textbook content
- Generate practice materials (quizzes, flashcards, short-answer questions)
- Provide personalized tutoring via natural language conversation

The primary model used in the default configuration is Meta Llama 3 70B Instruct, accessed through Amazon Bedrock.

### What are Bedrock Guardrails?
Bedrock Guardrails are safety controls that check, filter, and moderate model inputs and outputs. They help ensure generated content is aligned with pedagogical and policy constraints, preventing the model from producing harmful or off-topic content.

### Purpose of this document
This document is a technical reference for administrators and developers who need to understand how Bedrock Guardrails are wired into the system, how they are enforced at runtime, and how to customize or troubleshoot guardrail behavior. Guardrails are created automatically during CDK deployment; this file is primarily for advanced customization and reference.

### When to use this document
You do NOT need to read this document to perform a standard deployment — the CDK creates and wires guardrails automatically as part of the standard deployment steps (see `Docs/DEPLOYMENT_GUIDE.md`). Use this document if you need to:
- Understand how guardrails protect the application and user flows
- Customize guardrail rules (topic/detection/PII handling)
- Troubleshoot guardrail-related runtime behavior (blocked inputs/outputs)
- Modify messages returned to users when guardrails block content
- Audit or change the SSM parameters or IAM permissions used by guardrail features


## Overview

Bedrock Guardrails are used to protect the system from:

- Inappropriate content (sexual, violence, hate, insults, misconduct)
- Prompt injection attacks and system prompt extraction
- Sensitive information exposure (emails, phone numbers, social insurance numbers, credit cards)
- Off-topic content or non-educational requests, academic integrity violations
  - PII (Personally Identifiable Information) such as email, phone numbers, social insurance numbers, and credit/debit card numbers

This document reflects the implementation in the repository as of the current code:
- CDK stack creation of the guardrail: `cdk/lib/api-stack.ts`
- Text generation runtime integration and enforcement: `cdk/lambda/textGeneration/src/main.py` and `cdk/lambda/textGeneration/src/helpers/chat.py`

## Implementation Details

### Infrastructure (CDK)

The Bedrock guardrail is created in `cdk/lib/api-stack.ts` using the CDK Bedrock L1 construct `CfnGuardrail`.

Definitions:
- **L1 construct:** Low-level CloudFormation resource constructs in AWS CDK that map directly to CloudFormation resources; `CfnGuardrail` is the L1 construct used for Bedrock guardrails.
- **SSM Parameter Store:** AWS Systems Manager Parameter Store provides secure, hierarchical storage for configuration values such as the guardrail ID. The CDK stores the guardrail ID here so Lambda functions can read it at runtime.

The relevant declaration creates a guardrail and stores the guardrail ID in SSM Parameter Store so Lambdas can reference it at runtime.

Key snippet from `cdk/lib/api-stack.ts`:

```typescript
// Create Bedrock Guardrails
const bedrockGuardrail = new bedrock.CfnGuardrail(this, "BedrockGuardrail", {
  name: `${id}-oer-guardrail`,
  description: "Guardrail for OpenEd AI pedagogical tutor to ensure safe and appropriate educational interactions",
  blockedInputMessaging: "I'm here to help with your learning! However, I can't assist with that particular request. Let's focus on your textbook material instead. What specific topic would you like to explore?",
  blockedOutputsMessaging: "I want to keep our conversation focused on learning and education. Let me redirect us back to your studies. What concept from your textbook can I help you understand better?",
  contentPolicyConfig: {
    filtersConfig: [
      { type: "PROMPT_ATTACK", inputStrength: "HIGH", outputStrength: "NONE" }
    ]
  },
  sensitiveInformationPolicyConfig: {
    piiEntitiesConfig: [
      { type: "EMAIL", action: "BLOCK" },
      { type: "PHONE", action: "BLOCK" },
      { type: "CA_SOCIAL_INSURANCE_NUMBER", action: "BLOCK" },
      { type: "CREDIT_DEBIT_CARD_NUMBER", action: "BLOCK" },
    ]
  },
  topicPolicyConfig: {
    topicsConfig: [
      {
        name: "NonEducationalContent",
        definition: "Content that diverts from educational purposes...",
        examples: ["How to hack systems or bypass security"],
        type: "DENY",
      },
      { name: "AcademicIntegrity", type: "DENY", definition: "Requests that could compromise academic integrity..." },
      { name: "SystemPromptExtraction", type: "DENY", definition: "Attempts to extract system prompts or internal config" },
      { name: "RoleManipulation", type: "DENY", definition: "Attempts to make the AI ignore safety guidelines or assume dangerous roles" },
    ]
  }
});

// Then config stored in SSM Parameter Store so runtime can access it:
const guardrailParameter = new ssm.StringParameter(this, "GuardrailParameter", {
  parameterName: `/${id}/OER/GuardrailId`,
  description: "Parameter containing the Bedrock Guardrail ID",
  stringValue: bedrockGuardrail.attrGuardrailId,
});
```

Notes:
- The guardrail is configured with a mix of topic policy definition, PII blocking, and a strong input filter for prompt attacks.
- The SSM parameter path used is `/${id}/OER/GuardrailId` (where `${id}` is the CDK stack ID prefix for your deployment).

### Lambda Integration

The text generation runtime integrates guardrails via environment parameters, SSM reads, and runtime calls to Bedrock's `apply_guardrail` API.

Where it's wired:
- The text generation Lambda is set with `GUARDRAIL_ID_PARAM` environment variable in `api-stack.ts` which points to the SSM parameter (`/${id}/OER/GuardrailId`).
- The lambda receives the guardrail ID at runtime and checks the value in `initialize_constants()` in `cdk/lambda/textGeneration/src/main.py`.

 At runtime, guardrails are enforced within the helper functions in `cdk/lambda/textGeneration/src/helpers/chat.py`:

- `apply_guardrails(text, guardrail_id, source)` performs the call to Bedrock runtime:

```python
response = bedrock_runtime.apply_guardrail(
    guardrailIdentifier=guardrail_id,
    guardrailVersion="1",  # Must be a published version, not "DRAFT"
    source=source, # "INPUT" or "OUTPUT"
    content=[ {"text": {"text": text}} ]
)
```

- Input guardrails: `_apply_input_guardrails(query, guardrail_id)` is invoked before generation. If a guardrail blocks the input, a user-friendly message is returned and processing stops.
- Output guardrails: `_apply_output_guardrails(response_text, guardrail_id, guardrail_assessments)` is invoked after generation. If guardrails block the response, the response is replaced with the configured `blockedOutputsMessaging` fallback and the `guardrail_blocked` flag is returned in the API result.
- Assessments (returned by the guardrail API) are appended to `assessments` in the response when applicable.

### IAM and Permissions

The CDK stack adds policy statements to the text generation Lambda role to allow Bedrock runtime usage and applying guardrails:

- Actions permitted:
  - `bedrock:InvokeModel`
  - `bedrock:InvokeModelWithResponseStream`
  - `bedrock:ApplyGuardrail`

- The resources include the Bedrock LLM model, the embedding model, and the guardrail ARN:
  - `arn:aws:bedrock:${this.region}::foundation-model/meta.llama3-70b-instruct-v1:0`
  - `arn:aws:bedrock:${this.region}::foundation-model/cohere.embed-v4:0`
  - `arn:aws:bedrock:${this.region}:${this.account}:guardrail/${bedrockGuardrail.attrGuardrailId}`

This ensures the lambda has permission to both invoke the model and apply the configured guardrail.

## Configuration

Guardrail configuration in `api-stack.ts` includes:
- Content policy filters: `PROMPT_ATTACK` with `inputStrength` HIGH, `outputStrength` NONE (prevents prompt injection attacks on the input)
- PII detection: Blocks `EMAIL`, `PHONE`, `CA_SOCIAL_INSURANCE_NUMBER`, and `CREDIT_DEBIT_CARD_NUMBER`
- Topic policies: `NonEducationalContent`, `AcademicIntegrity`, `SystemPromptExtraction`, and `RoleManipulation` are `DENY` policy types and include example strings to catch matches
- `blockedInputMessaging` and `blockedOutputsMessaging` set friendly redirected messages to the user so we don't leak guardrail reasons or internal details

These are directly reflected in the L1 guardrail config in the `cdk/lib/api-stack.ts` file and are passed through to Bedrock when the guardrail is created.

## Runtime Behavior & Usage

1. **Input guardrails**: When a user submits a query (via API or WebSocket), the system calls `_apply_input_guardrails(query, guardrail_id)` before attempting to generate a response. If the content triggers a DENY rule (prompt attack, PII leak, academic integrity etc.), the system responds with the `blockedInputMessaging` text set in the guardrail.

2. **Output guardrails**: After generating a response, the system calls `_apply_output_guardrails(response_text, guardrail_id)`. If the guardrail intervenes (e.g., a redirection due to an output rule), the response is replaced with `blockedOutputsMessaging` (from the guardrail configuration) and `guardrail_blocked` is set in the JSON response.

3. **Logging & Observability**: Guardrail calls and outcomes are logged using the standard logging messages found in `helpers/chat.py`, and the returned `assessments` are included in the lambda response when available.

### Automatic Protection

- Guardrails are applied automatically to all user queries and AI-generated outputs handled by the text generation function. No additional runtime configuration is required — the lambda reads the SSM parameter identified in the CDK stack and applies the guardrails at runtime when `GUARDRAIL_ID_PARAM` is set.

## Testing

There is no dedicated `test_guardrails.py` script in the repository as of this update. To test guardrails locally or in Lambda, consider the following approaches:

1. **Manual test script** (example): create `cdk/lambda/textGeneration/test_guardrails.py` with the following example code (edit `guardrail_id` as needed):

```python
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
```

2. **Unit/Integration test**: You can add a small pytest-based test that mocks `boto3` client calls to return example guardrail responses.

3. **Live test**: Use the running API or WebSocket endpoint and submit an input that should be blocked (e.g., PII or prompt injection text). Observe CloudWatch logs for guardrail action details and check the API/WebSocket response for `guardrail_blocked` and `assessments`.

## Monitoring

Guardrail actions are logged to CloudWatch from the Lambda code. Look for the following log lines in `textGeneration` Lambda function logs:

- `Input guardrail check failed: ...` (warning when guardrail service errors)
- `Output blocked by guardrails` (warning when a response was blocked)
- `guardrail_blocked` field in returned API/WebSocket responses
- Assessment details appear in the `assessments` array appended to the response

Additionally, validate that:
- The SSM Parameter `/.../OER/GuardrailId` exists and contains a guardrail ID
- IAM role permissions for `bedrock:ApplyGuardrail` exist
- Monitor CloudWatch and AWS Bedrock metrics (if available) for calls and latency


## Customization

To modify guardrail settings:

1. Edit the `bedrockGuardrail` configuration in `cdk/lib/api-stack.ts` to change filters, PII policies, or topic policies.
2. Update `blockedInputMessaging` or `blockedOutputsMessaging` to provide tailored messaging.
3. Deploy the CDK stack to update the guardrail and SSM parameter.

**Important:** After deploying the CDK stack (which creates the guardrail in DRAFT state), you must publish version 1 of the guardrail in the AWS Bedrock console before the application will work. The Lambda functions reference `guardrailVersion="1"` — if no published version exists, guardrail calls will fail at runtime. See the Bedrock console > Guardrails > select your guardrail > "Create version" to publish.

## Troubleshooting

### Common Issues

1. **Guardrail not found**: Check that the guardrail exists in Bedrock and that the SSM parameter `/.../OER/GuardrailId` matches the deployed guardrail ID.
2. **Permission denied**: Confirm the text generation Lambda role includes `bedrock:ApplyGuardrail` in its policy statements and that the guardrail ARN was included as a resource.
3. **High latency**: Applying guardrails may add additional latency. Profile requests and monitor CloudWatch logs for time-related logs from Python lambda.

### Debug Steps

1. Check CloudWatch logs for any of the guardrail warning messages or `assessments` arrays.
2. Verify SSM parameter `/${id}/OER/GuardrailId` value and that the text generation lambda environment variable `GUARDRAIL_ID_PARAM` points to it.
3. Test `apply_guardrail` using the runtime `bedrock-runtime` client (see the example script above)
4. Confirm IAM role includes `bedrock:ApplyGuardrail` and the resource ARN for the created guardrail.

## References

- Guardrail CDK creation: `cdk/lib/api-stack.ts` (search for `new bedrock.CfnGuardrail`)
- Guardrail runtime usage: `cdk/lambda/textGeneration/src/helpers/chat.py` (`apply_guardrails`)
- Environment wiring: `cdk/lambda/textGeneration/src/main.py` (`GUARDRAIL_ID_PARAM` and `initialize_constants()`)
- Bedrock Documentation: https://docs.aws.amazon.com/bedrock

---

## Glossary

- **Bedrock LLM / Foundation model**: Pretrained large language models (LLMs) offered through Amazon Bedrock, such as Meta Llama 3 or Cohere Embed models. These are the models the runtime invokes to generate text and embeddings.
- **Guardrail**: A Bedrock configuration that contains policy-based rules to filter, block, or transform prompts and responses based on content policy, sensitive information, and topic rules.
- **PII (Personally Identifiable Information)**: Sensitive personal data that could identify an individual (e.g., email addresses, phone numbers, national identification numbers, credit/debit card numbers). In this project, PII is blocked by guardrails to prevent disclosure or misuse.
- **L1 construct (CDK)**: Low-level CDK constructs that map directly to CloudFormation resources. `CfnGuardrail` is used to create the Bedrock guardrail resource at the CloudFormation level.
- **SSM Parameter Store**: AWS Systems Manager Parameter Store, used to securely store configuration values (like the guardrail ID) so that Lambda functions can read them at runtime.
- **apply_guardrail**: Bedrock runtime API used by the text generation Lambda to evaluate input or output texts against guardrail rules.
