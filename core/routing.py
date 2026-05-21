"""
WebSocket URL routing for Django Channels.
Handles WebSocket connections and routes them to appropriate consumers.
"""

from django.urls import re_path
from manager.consumers import VMUpdatesConsumer

websocket_urlpatterns = [
    # WebSocket endpoint for real-time VM updates
    # Access at: ws://localhost:8000/ws/vms/updates/
    re_path(r'ws/vms/updates/$', VMUpdatesConsumer.as_asgi()),
]
