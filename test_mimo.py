# test_mimo.py
import os
from openai import OpenAI

client = OpenAI(
    api_key="sk-cyqvgto91h1khz21qprd0ynuw8wpsmuoox91tm4kblju9w1e",
    base_url="https://api.xiaomimimo.com/v1"
)

completion = client.chat.completions.create(
    model="mimo-v2-pro",
    messages=[
        {
            "role": "system",
            "content": "You are MiMo, an AI assistant developed by Xiaomi."
        },
        {
            "role": "user",
            "content": "你好，请介绍一下自己"
        }
    ],
    max_completion_tokens=1024,
    temperature=1.0,
    top_p=0.95,
    stream=False,
    stop=None,
    frequency_penalty=0,
    presence_penalty=0
)

print(completion.model_dump_json())
