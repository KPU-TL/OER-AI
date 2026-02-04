# Load Testing with Locust

## Overview

This directory contains load testing scripts for the OER-AI application using [Locust](https://locust.io/), an open-source load testing tool written in Python.

## What is Locust?

Locust is a scalable, distributed load testing framework that allows you to:

- Define user behavior in Python code
- Simulate thousands of concurrent users
- Monitor performance in real-time via web UI
- Generate detailed performance reports

**Key Features:**

- **Code-based**: Write test scenarios in Python (no XML/YAML)
- **Distributed**: Run tests across multiple machines
- **Real-time monitoring**: Web UI shows live statistics
- **Flexible**: Simulate complex user behaviors

## Installation

### Prerequisites

- Python 3.11+ installed
- pip package manager

### Install Locust

```bash
pip install locust websocket-client
```

Verify installation:

```bash
locust --version
```

## Test Files

### `locustfile.py`

Main load testing script that simulates realistic user behavior:

1. **User Session Creation** - Create authenticated user session
2. **Browse Textbooks** - List textbooks with pagination
3. **View Textbook Details** - Navigate to individual textbook pages
4. **Chat with LLM** - Create chat sessions and send messages via WebSocket
5. **View FAQ** - Fetch frequently asked questions for textbook
6. **Generate Practice Material** - Create practice questions/flashcards via WebSocket

**Task Weights:**

- Browse textbooks: 2 (low - periodic browsing)
- View textbook details: 10 (high - users spend most time here)
- Chat with LLM: 8 (high - primary user activity)
- View FAQ: 3 (medium - occasional reference)
- Generate practice material: 5 (medium-high - important feature)

## Running Load Tests

### Option 1: Web UI (Recommended for Development)

Start Locust with the web interface:

```bash
cd tests
locust -f locustfile.py --host=https://qscs7f1rm2.execute-api.ca-central-1.amazonaws.com
```

Then:

1. Open http://localhost:8089 in your browser
2. Set number of users (e.g., 10)
3. Set spawn rate (e.g., 1 user/second)
4. Click "Start swarming"

### Option 2: Headless Mode (CI/CD)

Run without web UI for automated testing:

```bash
cd tests
locust -f locustfile.py \
  --host=https://qscs7f1rm2.execute-api.ca-central-1.amazonaws.com \
  --headless \
  -u 10 \
  -r 1 \
  --run-time 5m
```

**Parameters:**

- `-u 10`: Simulate 10 concurrent users
- `-r 1`: Spawn 1 user per second
- `--run-time 5m`: Run for 5 minutes

### Option 3: Distributed Testing

For high-scale testing, run Locust in distributed mode:

**Master:**

```bash
locust -f locustfile.py --master --host=https://qscs7f1rm2.execute-api.ca-central-1.amazonaws.com
```

**Workers (run on multiple machines):**

```bash
locust -f locustfile.py --worker --master-host=<master-ip>
```

## Monitoring Results

### Web UI Metrics

When using the web UI, you'll see:

- **Statistics Table**: Request counts, response times (median, 95th percentile), error rates
- **Charts**: Real-time graphs of RPS (requests per second) and response times
- **Failures**: Detailed error messages and counts
- **Current Users**: Number of active simulated users

### Console Output

In headless mode, Locust prints periodic statistics:

```
Type     Name                                          # reqs      # fails  |     Avg     Min     Max  Median  |   req/s failures/s
--------|---------------------------------------------|-------|-------------|-------|-------|-------|-------|--------|-----------
GET      /public/config/welcomeMessage                    45     0(0.00%)  |     120      89     250     110  |    1.50        0.00
GET      /textbooks                                      112     0(0.00%)  |     135      95     310     130  |    3.73        0.00
GET      /textbooks/{id}                                 225     0(0.00%)  |     142     100     340     140  |    7.50        0.00
POST     /textbooks/{id}/chat_sessions (create)           89     0(0.00%)  |     156     110     380     150  |    2.97        0.00
--------|---------------------------------------------|-------|-------------|-------|-------|-------|-------|--------|-----------
         Aggregated                                      471     0(0.00%)  |     140      89     380     135  |   15.70        0.00
```

## CloudWatch Monitoring

While load testing, monitor these CloudWatch metrics:

### API Gateway

- **Count**: Total requests
- **Latency**: Response times (p50, p90, p99)
- **4XXError**: Client errors (401, 403, 429)
- **5XXError**: Server errors

### Lambda Functions

- **Invocations**: Number of calls
- **Duration**: Execution time
- **Errors**: Failed invocations
- **Throttles**: Rate-limited requests
- **ConcurrentExecutions**: Active instances

### RDS Database

- **DatabaseConnections**: Active connections
- **CPUUtilization**: CPU usage
- **ReadLatency / WriteLatency**: Query performance

### CloudWatch Logs

Check these log groups:

- `<StackPrefix>-ApiAccessLogs` - API access logs
- `API-Gateway-Execution-Logs_<api-id>/prod` - API execution logs
- `/aws/lambda/<function-name>` - Lambda function logs
- `/aws/rds/instance/<instance-id>/postgresql` - RDS logs

## Test Scenarios Implemented

### 1. Basic User Flow

- ✅ Fetch welcome message
- ✅ Browse textbooks with pagination
- ✅ View individual textbook details
- ✅ Token refresh on expiration

### 2. WebSocket Chat

- ✅ Create user session via API
- ✅ Create chat session for textbook
- ✅ Establish WebSocket connection with token auth
- ✅ Send chat messages via WebSocket
- ✅ Handle streaming responses (start → chunks → complete)
- ✅ Heartbeat/ping-pong mechanism
- ✅ Token refresh after user session creation

### 3. FAQ and Practice Material

- ✅ Fetch FAQ list for textbook
- ✅ Handle 404 gracefully (no FAQs yet)
- ✅ Generate practice materials via WebSocket
- ✅ Track practice material progress (initializing → retrieving → generating → validating → complete)
- ✅ Support multiple material types (MCQ, flashcard, short answer)
- ✅ Random difficulty selection (beginner, intermediate, advanced)

### 4. Error Handling

- ✅ Automatic token refresh on 401 errors
- ✅ WebSocket reconnection on disconnect
- ✅ Graceful handling of 404 errors
- ✅ Response validation and error reporting

## Performance Baselines

Based on initial testing:

| Endpoint                        | Avg Response Time | 95th Percentile | Notes                          |
| ------------------------------- | ----------------- | --------------- | ------------------------------ |
| `/public/config/welcomeMessage` | ~120ms            | ~200ms          | Simple config fetch            |
| `/user_sessions` (POST)         | ~150ms            | ~250ms          | User session creation          |
| `/textbooks`                    | ~135ms            | ~250ms          | Database query with pagination |
| `/textbooks/{id}`               | ~142ms            | ~280ms          | Single textbook lookup         |
| `/textbooks/{id}/faq`           | ~140ms            | ~260ms          | FAQ list fetch                 |
| Chat session creation           | ~156ms            | ~300ms          | Database insert                |
| WebSocket chat                  | ~2-5s             | ~8s             | LLM generation (streaming)     |
| WebSocket practice material     | ~3-8s             | ~12s            | LLM generation with validation |

## Troubleshooting

### Common Issues

**1. Token Fetch Fails**

```
✗ Failed to fetch token. Status: 403
```

**Solution**: Check API Gateway endpoint and ensure `/user/publicToken` is accessible.

**2. WebSocket Connection Fails**

```
[WebSocket] ✗ Error: Connection refused
```

**Solution**: Verify WebSocket URL and ensure token is valid.

**3. 429 Too Many Requests**

```
Got status code 429
```

**Solution**: Reduce number of users or spawn rate. Check API Gateway throttling limits.

**4. Chat Session Creation Fails**

```
Got status code 404
```

**Solution**: Ensure textbook ID exists. Check that endpoint path is correct.

### Debug Mode

Enable verbose logging:

```bash
locust -f locustfile.py --host=... --loglevel DEBUG
```

## Best Practices

### Load Testing Guidelines

1. **Start Small**: Begin with 1-5 users to verify functionality
2. **Ramp Gradually**: Increase load slowly to find breaking points
3. **Monitor CloudWatch**: Watch for errors, throttling, and high latency
4. **Test Realistic Scenarios**: Match actual user behavior patterns
5. **Run During Off-Peak**: Avoid impacting real users
6. **Document Results**: Record baselines and performance changes

### Recommended Test Progression

```bash
# 1. Smoke test (verify functionality)
locust -f locustfile.py --host=... --headless -u 1 -r 1 --run-time 1m

# 2. Light load (baseline performance)
locust -f locustfile.py --host=... --headless -u 5 -r 1 --run-time 5m

# 3. Medium load (typical usage)
locust -f locustfile.py --host=... --headless -u 10 -r 1 --run-time 10m

# 4. Stress test (find limits)
locust -f locustfile.py --host=... --headless -u 20 -r 2 --run-time 15m

# 5. Spike test (sudden traffic) - CAUTION: May trigger WAF/rate limits
locust -f locustfile.py --host=... --headless -u 50 -r 5 --run-time 5m
```

> **⚠️ Important**: High user counts (50+) or fast spawn rates (10+/sec) may trigger WAF rules or API Gateway rate limits, resulting in 403 errors. Start small and gradually increase load to find safe limits.

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Load Test

on:
  schedule:
    - cron: "0 2 * * *" # Daily at 2 AM
  workflow_dispatch:

jobs:
  load-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          pip install locust websocket-client

      - name: Run load test
        run: |
          cd tests
          locust -f locustfile.py \
            --host=https://qscs7f1rm2.execute-api.ca-central-1.amazonaws.com \
            --headless \
            -u 10 \
            -r 1 \
            --run-time 5m \
            --html=report.html

      - name: Upload report
        uses: actions/upload-artifact@v3
        with:
          name: load-test-report
          path: tests/report.html
```

## Additional Resources

- [Locust Documentation](https://docs.locust.io/)
- [WebSocket Testing Guide](https://docs.locust.io/en/stable/testing-other-systems.html#websockets)
- [AWS CloudWatch Metrics](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/working_with_metrics.html)
- [API Gateway Throttling](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-request-throttling.html)

## Support

For issues or questions:

1. Check CloudWatch logs for errors
2. Review Locust console output
3. Verify API endpoints in Swagger definition
4. Check network connectivity and authentication
