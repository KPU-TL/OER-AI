"""
Locust load testing script for OER-AI application.

This script simulates realistic user behavior:
1. Fetch welcome message (landing page)
2. Browse textbooks (list view)
3. Navigate to individual textbook pages
4. Chat with LLM via WebSocket

To run this test:
    locust -f locustfile.py --host=https://qscs7f1rm2.execute-api.ca-central-1.amazonaws.com

The script automatically fetches a public JWT token from the API.
"""

from locust import HttpUser, task, between, events
import json
import requests
import random
import websocket
import threading
import time
import uuid

# API endpoint for fetching public token
API_HOST = "https://qscs7f1rm2.execute-api.ca-central-1.amazonaws.com"
PUBLIC_TOKEN_ENDPOINT = f"{API_HOST}/prod/user/publicToken"
WEBSOCKET_URL = "wss://0m5o46nhs6.execute-api.ca-central-1.amazonaws.com/prod"


def get_public_token():
    """Fetch a public JWT token from the API."""
    try:
        print(f"Fetching token from: {PUBLIC_TOKEN_ENDPOINT}")
        # Use GET request - the endpoint doesn't require a body
        response = requests.get(PUBLIC_TOKEN_ENDPOINT, timeout=10)
        print(f"Response status: {response.status_code}")
        print(f"Response body: {response.text[:200]}")
        
        if response.status_code == 200:
            data = response.json()
            token = data.get("token")
            if token:
                print(f"✓ Successfully fetched public token: {token[:30]}...")
                return token
            else:
                print("✗ No token found in response")
                print(f"Response data: {data}")
                return None
        else:
            print(f"✗ Failed to fetch token. Status: {response.status_code}")
            print(f"Response: {response.text}")
            return None
    except Exception as e:
        print(f"✗ Error fetching token: {e}")
        import traceback
        traceback.print_exc()
        return None


# Fetch token once when the module loads
print("\n" + "="*60)
print("Initializing Locust - Fetching authentication token...")
print("="*60)
TOKEN = get_public_token()

if TOKEN:
    print(f"✓ Token loaded successfully")
else:
    print("✗ WARNING: Failed to fetch token. Tests will likely fail.")
    print("  Please check your network connection and API endpoint.")
print("="*60 + "\n")


class OERWebsiteUser(HttpUser):
    """
    Simulates a user browsing the OER-AI website and chatting with the LLM.
    
    Realistic user flow:
    1. View welcome message (landing page)
    2. Browse textbooks (list)
    3. Navigate to specific textbook pages
    4. Create chat session and chat with LLM via WebSocket
    """
    
    # Wait 2-5 seconds between actions (simulating reading/thinking)
    wait_time = between(2, 5)
    
    def on_start(self):
        """Called when a simulated user starts. Sets up authentication and initial data."""
        if not TOKEN:
            raise ValueError("No authentication token available. Cannot start user simulation.")
        
        self.token = TOKEN
        self.textbook_ids = []  # Store textbook IDs for navigation
        self.selected_textbook_id = None  # Stick to one textbook per user
        self.chat_session_id = None  # Single chat session for the selected textbook
        self.user_session_id = str(uuid.uuid4())  # Generate unique user session ID
        self.ws = None
        self.ws_connected = False
        self.ws_messages = []  # Store received WebSocket messages
        self.update_headers()
        print(f"User started with token: {self.token[:20]}... and session: {self.user_session_id}")
        
        # Simulate landing page - fetch welcome message
        self.get_welcome_message_initial()
        
        # Fetch initial textbook list
        self.fetch_initial_textbooks()
        
        # Initialize WebSocket connection
        self.connect_websocket()
    
    def on_stop(self):
        """Called when user stops. Clean up WebSocket connection."""
        if self.ws:
            try:
                self.ws.close()
            except:
                pass
    
    def update_headers(self):
        """Update request headers with current token."""
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
    
    def refresh_token_if_needed(self, response):
        """Check if token expired and refresh ONLY if needed."""
        if response.status_code == 401:
            print("⚠ Token expired (401), fetching new token...")
            new_token = get_public_token()
            if new_token:
                global TOKEN
                TOKEN = new_token
                self.token = new_token
                self.update_headers()
                print("✓ Token refreshed successfully")
                # Reconnect WebSocket with new token
                self.connect_websocket()
                return True
            else:
                print("✗ Failed to refresh token")
                return False
        return False
    
    def connect_websocket(self):
        """Establish WebSocket connection for chat."""
        try:
            if self.ws:
                try:
                    self.ws.close()
                except:
                    pass
            
            ws_url = f"{WEBSOCKET_URL}?token={self.token}"
            print(f"[WebSocket] Connecting to: {ws_url[:80]}...")
            
            self.ws = websocket.WebSocketApp(
                ws_url,
                on_message=self.on_ws_message,
                on_error=self.on_ws_error,
                on_open=self.on_ws_open,
                on_close=self.on_ws_close
            )
            
            # Run WebSocket in background thread
            self.ws_thread = threading.Thread(target=self.ws.run_forever)
            self.ws_thread.daemon = True
            self.ws_thread.start()
            
            # Wait a bit for connection to establish
            time.sleep(1)
        except Exception as e:
            print(f"[WebSocket] Error connecting: {e}")
    
    def on_ws_open(self, ws):
        """WebSocket connection opened."""
        self.ws_connected = True
        print("[WebSocket] ✓ Connected")
    
    def on_ws_message(self, ws, message):
        """Handle incoming WebSocket messages."""
        try:
            data = json.loads(message)
            msg_type = data.get("type")
            
            if msg_type == "pong":
                # Heartbeat response
                return
            elif msg_type == "start":
                print("[WebSocket] ← Stream started")
            elif msg_type == "chunk":
                # Accumulate chunks (don't print each one to avoid spam)
                pass
            elif msg_type == "complete":
                sources = data.get("sources", [])
                print(f"[WebSocket] ← Stream complete (sources: {len(sources)})")
            elif msg_type == "error":
                error_msg = data.get("message", "Unknown error")
                print(f"[WebSocket] ✗ Error: {error_msg}")
            
            self.ws_messages.append(data)
        except Exception as e:
            print(f"[WebSocket] Error parsing message: {e}")
    
    def on_ws_error(self, ws, error):
        """WebSocket error handler."""
        print(f"[WebSocket] ✗ Error: {error}")
    
    def on_ws_close(self, ws, close_status_code, close_msg):
        """WebSocket connection closed."""
        self.ws_connected = False
        print(f"[WebSocket] Disconnected (code: {close_status_code})")
    
    def send_ws_message(self, message):
        """Send a message via WebSocket."""
        if self.ws and self.ws_connected:
            try:
                self.ws.send(json.dumps(message))
                return True
            except Exception as e:
                print(f"[WebSocket] Error sending message: {e}")
                return False
        else:
            print("[WebSocket] Not connected, cannot send message")
            return False
    
    def get_welcome_message_initial(self):
        """Fetch welcome message on initial page load (not a task)."""
        with self.client.get(
            "/prod/public/config/welcomeMessage",
            headers=self.headers,
            catch_response=True,
            name="/public/config/welcomeMessage"
        ) as response:
            if response.status_code == 200:
                try:
                    data = response.json()
                    print(f"✓ Welcome message: {data.get('welcomeMessage', '')[:50]}...")
                    response.success()
                except json.JSONDecodeError:
                    response.failure("Failed to parse welcome message JSON")
            else:
                response.failure(f"Got status code {response.status_code}")
    
    def fetch_initial_textbooks(self):
        """Fetch initial textbook list on page load (not a task)."""
        with self.client.get(
            "/prod/textbooks?limit=20&offset=0",
            headers=self.headers,
            catch_response=True,
            name="/textbooks (initial)"
        ) as response:
            if response.status_code == 200:
                try:
                    data = response.json()
                    textbooks = data.get("textbooks", [])
                    self.textbook_ids = [tb.get("id") for tb in textbooks if tb.get("id")]
                    # Select the first textbook and stick with it
                    if self.textbook_ids and not self.selected_textbook_id:
                        self.selected_textbook_id = self.textbook_ids[0]
                        print(f"✓ Selected textbook: {self.selected_textbook_id[:20]}...")
                    print(f"✓ Loaded {len(self.textbook_ids)} textbook IDs")
                    response.success()
                except json.JSONDecodeError:
                    response.failure("Failed to parse textbooks JSON")
            elif self.refresh_token_if_needed(response):
                response.failure("Token expired and refreshed")
            else:
                response.failure(f"Got status code {response.status_code}")
    
    @task(2)
    def get_welcome_message(self):
        """
        Occasionally re-fetch welcome message.
        Lower weight (2) since users don't refresh the landing page often.
        """
        with self.client.get(
            "/prod/public/config/welcomeMessage",
            headers=self.headers,
            catch_response=True,
            name="/public/config/welcomeMessage"
        ) as response:
            if response.status_code == 200:
                response.success()
            elif self.refresh_token_if_needed(response):
                response.failure("Token expired and refreshed")
            else:
                response.failure(f"Got status code {response.status_code}")
    
    @task(5)
    def browse_textbooks(self):
        """
        Browse textbooks list (with pagination).
        Medium weight (5) - users browse the list periodically.
        """
        # Randomly choose an offset for pagination
        offset = random.choice([0, 20, 40])
        
        with self.client.get(
            f"/prod/textbooks?limit=20&offset={offset}",
            headers=self.headers,
            catch_response=True,
            name="/textbooks"
        ) as response:
            if response.status_code == 200:
                try:
                    data = response.json()
                    textbooks = data.get("textbooks", [])
                    # Update our list of textbook IDs
                    new_ids = [tb.get("id") for tb in textbooks if tb.get("id")]
                    if new_ids:
                        self.textbook_ids = new_ids
                    print(f"✓ Browsed {len(textbooks)} textbooks (offset={offset})")
                    response.success()
                except json.JSONDecodeError:
                    response.failure("Failed to parse JSON response")
            elif self.refresh_token_if_needed(response):
                response.failure("Token expired and refreshed")
            else:
                response.failure(f"Got status code {response.status_code}")
    
    @task(10)
    def view_textbook_detail(self):
        """
        View individual textbook details.
        High weight (10) - users spend most time on textbook pages.
        """
        if not self.selected_textbook_id:
            print("⚠ No textbook selected, fetching list first...")
            self.fetch_initial_textbooks()
            return
        
        # Use the selected textbook (not random)
        textbook_id = self.selected_textbook_id
        
        with self.client.get(
            f"/prod/textbooks/{textbook_id}",
            headers=self.headers,
            catch_response=True,
            name="/textbooks/{id}"
        ) as response:
            if response.status_code == 200:
                try:
                    data = response.json()
                    title = data.get("title", "Unknown")
                    print(f"✓ Viewing textbook: {title}")
                    response.success()
                except json.JSONDecodeError:
                    response.failure("Failed to parse JSON response")
            elif response.status_code == 404:
                print(f"⚠ Textbook {textbook_id} not found, removing from list")
                if textbook_id in self.textbook_ids:
                    self.textbook_ids.remove(textbook_id)
                response.failure("Textbook not found")
            elif self.refresh_token_if_needed(response):
                response.failure("Token expired and refreshed")
            else:
                response.failure(f"Got status code {response.status_code}")
    
    @task(8)
    def chat_with_llm(self):
        """
        Chat with LLM about a textbook via WebSocket.
        High weight (8) - this is a primary user activity.
        """
        if not self.selected_textbook_id:
            print("⚠ No textbook selected for chat")
            return
        
        if not self.ws_connected:
            print("⚠ WebSocket not connected, skipping chat")
            return
        
        # Use the selected textbook (not random)
        textbook_id = self.selected_textbook_id
        
        # Create or get chat session for this textbook
        if not self.chat_session_id:
            # Create new chat session
            with self.client.post(
                f"/prod/textbooks/{textbook_id}/chat_sessions",
                headers=self.headers,
                json={"user_sessions_session_id": self.user_session_id},
                catch_response=True,
                name="/textbooks/{id}/chat_sessions (create)"
            ) as response:
                if response.status_code == 201:
                    try:
                        data = response.json()
                        chat_session_id = data.get("chat_session_id")
                        if chat_session_id:
                            self.chat_session_id = chat_session_id
                            print(f"✓ Created chat session: {chat_session_id[:20]}...")
                            response.success()
                        else:
                            response.failure("No chat_session_id in response")
                            return
                    except json.JSONDecodeError:
                        response.failure("Failed to parse JSON response")
                        return
                elif self.refresh_token_if_needed(response):
                    response.failure("Token expired and refreshed")
                    return
                else:
                    # Log the error details for debugging
                    try:
                        error_data = response.json()
                        print(f"✗ Chat session creation failed: {error_data}")
                    except:
                        print(f"✗ Chat session creation failed: {response.text}")
                    response.failure(f"Got status code {response.status_code}")
                    return
        
        if not self.chat_session_id:
            return
        
        # Send a message via WebSocket
        queries = [
            "Hi, explain this textbook to me",
            "What are the main topics covered?",
            "Can you summarize the key concepts?",
            "What should I focus on first?",
            "How is this textbook structured?"
        ]
        query = random.choice(queries)
        
        message = {
            "action": "generate_text",
            "textbook_id": textbook_id,
            "query": query,
            "chat_session_id": self.chat_session_id
        }
        
        print(f"[WebSocket] → Sending: '{query[:50]}...'")
        success = self.send_ws_message(message)
        
        if success:
            # Wait a bit for response to start streaming
            time.sleep(0.5)
        else:
            print("[WebSocket] Failed to send message")
