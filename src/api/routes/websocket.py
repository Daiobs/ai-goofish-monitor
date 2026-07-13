"""
WebSocket 路由
提供实时通信功能
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from typing import Set

from src.api.auth import read_websocket_session


router = APIRouter()

# 全局 WebSocket 连接管理
active_connections: Set[WebSocket] = set()


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
):
    """WebSocket 端点"""
    if read_websocket_session(websocket) is None:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    active_connections.add(websocket)

    try:
        # 保持连接并接收消息
        while True:
            # 接收客户端消息（如果有的话）
            data = await websocket.receive_text()
            # 这里可以处理客户端发送的消息
            # 目前我们主要用于服务端推送，所以暂时不处理
    except WebSocketDisconnect:
        active_connections.discard(websocket)
    except Exception:
        active_connections.discard(websocket)


async def broadcast_message(message_type: str, data: dict):
    """向所有连接的客户端广播消息"""
    message = {
        "type": message_type,
        "data": data
    }

    # 移除已断开的连接
    disconnected = set()

    for connection in active_connections:
        try:
            await connection.send_json(message)
        except Exception:
            disconnected.add(connection)

    # 清理断开的连接
    for connection in disconnected:
        active_connections.discard(connection)
