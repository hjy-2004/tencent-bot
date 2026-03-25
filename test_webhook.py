# test_webhook.py
import httpx
import asyncio
import json

async def test():
    """模拟腾讯平台发送 @机器人 消息"""
    payload = {
        "op": 0,
        "t": "AT_MESSAGE_CREATE",
        "d": {
            "id": "test_msg_001",
            "content": "你好MiMo，请介绍一下你自己",
            "author": {
                "id": "user_12345",
                "username": "测试用户"
            },
            "channel_id": "channel_001",
            "guild_id": "guild_001"
        }
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "http://localhost:8080/api/v1/webhook",
            json=payload,
        )
        print(f"状态码: {resp.status_code}")
        print(f"响应: {json.dumps(resp.json(), ensure_ascii=False, indent=2)}")

asyncio.run(test())
