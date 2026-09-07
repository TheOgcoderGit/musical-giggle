import subprocess
result = subprocess.run(['grep', '-n', 'async def button_handler', '/home/user/project/channelflow/extracted/ChannelFlowAI5_monetization/bot/handlers.py'], capture_output=True, text=True)
print(result.stdout)