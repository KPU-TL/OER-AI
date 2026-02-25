const { LambdaClient, InvokeCommand } = require("@aws-sdk/client-lambda");

const lambda = new LambdaClient({});

exports.handler = async (event) => {
  console.log("WebSocket message received:", {
    connectionId: event.requestContext.connectionId,
    routeKey: event.requestContext.routeKey,
    body: event.body,
    timestamp: new Date().toISOString(),
  });

  try {
    const body = JSON.parse(event.body);
    const { action, textbook_id, query, chat_session_id } = body;

    if (action === "generate_text") {
      // Invoke the text generation Lambda function
      const textGenPayload = {
        pathParameters: {
          id: chat_session_id,
        },
        body: JSON.stringify({
          textbook_id: textbook_id,
          query: query,
        }),
        requestContext: {
          connectionId: event.requestContext.connectionId,
          domainName: event.requestContext.domainName,
          stage: event.requestContext.stage,
        },
      };

      console.log(
        "Invoking text generation function with payload:",
        textGenPayload,
      );

      const result = await lambda.send(
        new InvokeCommand({
          FunctionName: process.env.TEXT_GEN_FUNCTION_NAME,
          InvocationType: "Event", // Asynchronous invocation
          Payload: JSON.stringify(textGenPayload),
        }),
      );

      console.log("Text generation function invoked successfully:", result);

      return { statusCode: 200 };
    }

    if (action === "generate_practice_material") {
      // Extract practice material specific fields
      const {
        material_type,
        topic,
        difficulty,
        num_questions,
        num_options,
        num_cards,
        card_type,
        force_fresh,
      } = body;

      // Validate required fields
      if (
        !textbook_id ||
        typeof textbook_id !== "string" ||
        textbook_id.trim() === ""
      ) {
        console.log("Missing or invalid textbook_id field");
        return {
          statusCode: 400,
          body: JSON.stringify({ error: "textbook_id is required" }),
        };
      }

      if (!topic || typeof topic !== "string" || topic.trim() === "") {
        console.log("Missing or invalid topic field");
        return {
          statusCode: 400,
          body: JSON.stringify({ error: "Topic is required" }),
        };
      }

      const practicePayload = {
        pathParameters: {
          textbook_id: textbook_id,
        },
        body: JSON.stringify({
          material_type: material_type || "mcq",
          topic: topic,
          difficulty: difficulty || "intermediate",
          num_questions: num_questions || 5,
          num_options: num_options || 4,
          num_cards: num_cards || 10,
          card_type: card_type || "definition",
          force_fresh: force_fresh || false,
        }),
        httpMethod: "POST",
        resource: "/textbooks/{textbook_id}/practice_materials",
        // WebSocket context for sending progress updates
        requestContext: {
          connectionId: event.requestContext.connectionId,
          domainName: event.requestContext.domainName,
          stage: event.requestContext.stage,
        },
        isWebSocket: true, // Flag to indicate WebSocket invocation
      };

      console.log(
        "Invoking practice material function with payload:",
        practicePayload,
      );

      const result = await lambda.send(
        new InvokeCommand({
          FunctionName: process.env.PRACTICE_MATERIAL_FUNCTION_NAME,
          InvocationType: "Event", // Asynchronous invocation
          Payload: JSON.stringify(practicePayload),
        }),
      );

      console.log("Practice material function invoked successfully:", result);

      return { statusCode: 200 };
    }

    // Handle warmup requests - invoke both text generation and practice material Lambdas to pre-warm them
    if (action === "warmup") {
      console.log("Warmup request received");

      const warmupPayload = {
        warmup: true, // Flag to trigger early return in Lambda
        textbook_id: textbook_id, // Pass textbook_id for context if needed
      };

      // Warm up text generation Lambda (primary target for chat)
      try {
        await lambda.send(
          new InvokeCommand({
            FunctionName: process.env.TEXT_GEN_FUNCTION_NAME,
            InvocationType: "Event", // Fire-and-forget
            Payload: JSON.stringify(warmupPayload),
          }),
        );
        console.log("Text generation warmup invocation sent successfully");
      } catch (warmupError) {
        console.warn("Text generation warmup invocation failed:", warmupError);
        // Don't fail the request - warmup is best-effort
      }

      // Also warm up practice material Lambda
      try {
        await lambda.send(
          new InvokeCommand({
            FunctionName: process.env.PRACTICE_MATERIAL_FUNCTION_NAME,
            InvocationType: "Event", // Fire-and-forget
            Payload: JSON.stringify(warmupPayload),
          }),
        );
        console.log("Practice material warmup invocation sent successfully");
      } catch (warmupError) {
        console.warn(
          "Practice material warmup invocation failed:",
          warmupError,
        );
        // Don't fail the request - warmup is best-effort
      }

      return { statusCode: 200, body: JSON.stringify({ status: "warming" }) };
    }

    return {
      statusCode: 400,
      body: JSON.stringify({ error: "Unknown action" }),
    };
  } catch (error) {
    console.error("Error processing WebSocket message:", error);
    return {
      statusCode: 500,
      body: JSON.stringify({ error: "Internal server error" }),
    };
  }
};
