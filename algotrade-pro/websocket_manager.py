"""WebSocket connection manager for real-time dashboard updates."""

import json
import logging
from fastapi import WebSocket

logger = logging.getLogger("algotrade.ws")


class ConnectionManager:
    """Manages active WebSocket connections and broadcasts events."""

    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info("WS client connected (%d total)", len(self.active_connections))

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)
        logger.info("WS client disconnected (%d remaining)", len(self.active_connections))

    async def broadcast(self, event: str, data: dict):
        """Send a JSON event to every connected client."""
        message = json.dumps({"event": event, "data": data})
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                disconnected.append(connection)
        for conn in disconnected:
            self.active_connections.remove(conn)

    async def broadcast_trade(self, trade_data: dict):
        await self.broadcast("trade_executed", trade_data)

    async def broadcast_balance_update(self, balance_data: dict):
        await self.broadcast("balance_update", balance_data)


ws_manager = ConnectionManager()
