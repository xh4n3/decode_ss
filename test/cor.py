import asyncio
from time import sleep


@asyncio.coroutine
def hello_world():
    print("H")
    sleep(3)
    print("Hello World!")
loop = asyncio.get_event_loop()
# Blocking call which returns when the hello_world() coroutine is done
print('1')
loop.run_until_complete(hello_world())
print('2')
loop.close()
