"""
Try `stcode` package
"""

import asyncio
from stcode.core.llm_gateway import LLMGateway
from stcode.core.providers.types import Message


gw = LLMGateway()


async def main():
    test_msg = [
        Message(
            role="user",
            content="Hi"
        )
    ]

    async for evt in gw.stream(test_msg):
        print(evt)

if __name__ == "__main__":
    asyncio.run(main())
