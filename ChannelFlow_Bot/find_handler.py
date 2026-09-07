content = open('bot/handlers.py').read()
for i, line in enumerate(content.split('\n')):
    if 'async def button_handler' in line:
        print(f"Line {i+1}: {line}")