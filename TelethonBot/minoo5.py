import asyncio
import aiofiles
import io
import os
import pathlib
import sys
from typing import Iterable, Iterator, Optional, BinaryIO

import typing

from more_itertools import grouper 
from telethon import events 
from telethon.tl import types
from telethon.tl.types import MessageMediaPhoto, ChannelMessagesFilter
from telethon.tl import functions, types
from telethon import TelegramClient, utils, helpers 
from apscheduler.schedulers.asyncio import AsyncIOScheduler 
from telethon.client.downloads import MIN_CHUNK_SIZE 
from telethon.crypto import AES
from telethon.errors.rpcerrorlist import FloodWaitError, FileReferenceExpiredError
from telethon.errors import ApiIdInvalidError
from telethon.sessions import StringSession

if sys.version_info < (3, 8):
    cached_property = property
else:
    from functools import cached_property

# Additional imports
import logging
from tqdm import tqdm
import time

# Set up your Telethon API credentials
api_id = 12640295
api_hash = '5577976420b6949e4135cea330148cdd'
bot_token = '5757237888:AAE9DtXZGdH27RG_5uCleQKWnZcW7CBkuiA'

# Set up your Telegram account credentials
phone_number = '+256701330009'

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Create a Telethon client
client = TelegramClient(None, api_id, api_hash)

# Define ID checker and validator functions
async def is_valid_entity(entity_id):
    try:
        entity = await client.get_entity(entity_id)
        return True if entity else False
    except ValueError:
        return False

async def validate_entity(entity_id, event):
    if not await is_valid_entity(entity_id):
        await event.respond(f'Invalid chat or channel ID: {entity_id}. Please check the ID and try again.')
        return False
    return True

async def get_input_entity(entity_id):
    try:
        entity = await client.get_input_entity(entity_id)
        return entity
    except ValueError:
        logger.error('Could not find the input entity for entity_id: %s', entity_id)
        return None

async def connect_account():
    if not await client.is_user_authorized():
        await client.send_code_request(phone_number)
        await client.sign_in(phone_number, input('Enter the code you received: '))

# Token bucket algorithm for rate limiting
class TokenBucket:
    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.tokens = capacity
        self.refill_rate = refill_rate
        self.last_refill_time = time.time()

    async def consume(self, amount: int):
        while self.tokens < amount:
            await asyncio.sleep(1)
            self.refill()
        self.tokens -= amount

    def refill(self):
        now = time.time()
        elapsed_time = now - self.last_refill_time
        tokens_to_add = elapsed_time * self.refill_rate
        self.tokens = min(self.capacity, self.tokens + tokens_to_add)
        self.last_refill_time = now

# Token bucket for handling API request limit
api_rate_limit = TokenBucket(capacity=30, refill_rate=0.5)  # Adjust capacity and refill rate as needed

# Handle the "/getchannel" command and send a list of available channels
@client.on(events.NewMessage(pattern='/getchannel'))
async def get_channel_ids(event):
    await api_rate_limit.consume(1)  # Consume a token for each API request
    logger.info('Received /getchannel command from user: %s', event.sender_id)

    # Fetch a list of dialogs (channels)
    dialogs = await client.get_dialogs()

    # Create a string containing the channel IDs
    channel_ids = "List of available channels:\n"
    for dialog in dialogs:
        if isinstance(dialog.entity, types.Channel):
            channel_ids += f"{dialog.name} - ID: {dialog.id}\n"

    # Split the channel IDs into smaller chunks
    chunk_size = 4096
    chunks = [channel_ids[i:i + chunk_size] for i in range(0, len(channel_ids), chunk_size)]

    # Send each chunk as a separate message
    for chunk in chunks:
        await event.respond(chunk)

# Define a dictionary to store downloaded messages
downloaded_messages = {}

# Handle the "/clone" command
@client.on(events.NewMessage(pattern='/clone'))
async def clone_messages(event):
    await api_rate_limit.consume(1)  # Consume a token for each API request
    logger.info('Received /clone command from user: %s', event.sender_id)

    # Extract the parameters from the command message
    command = event.message.text.strip().split(' ')
    if len(command) < 4:
        await event.respond('Invalid command format. Please use the format: /clone SOURCE_CHAT_ID -> TARGET_CHAT_ID [-> START_MESSAGE_ID] [-> LIMIT]')
        return

    try:
        source_chat_id = int(command[1])
        target_chat_id = int(command[3])
        start_id = int(command[5]) if len(command) > 5 else None
        limit = int(command[7]) if len(command) > 7 else None
    except (ValueError, IndexError):
        await event.respond('Invalid command format. Please use the format: /clone SOURCE_CHAT_ID -> TARGET_CHAT_ID [-> START_MESSAGE_ID] [-> LIMIT]')
        return

    logger.info('Starting clone task for user: %s', event.sender_id)
    await event.respond('🔧 Setting up Clone:\n'
                        f'— From Source: {source_chat_id}\n'
                        f'— To Target: {target_chat_id}\n'
                        f'— Start From Message Id: {start_id if start_id else "Not specified"}\n'
                        f'— Limit Message: {limit if limit else "No limit"}\n'
                        '— Delay: 4 sec\n'
                        '==== Process ====')

    # Validate source and target chat IDs
    if not await validate_entity(source_chat_id, event) or not await validate_entity(target_chat_id, event):
        return

    # Get the input entities for source and target chats
    source_entity = await get_input_entity(source_chat_id)
    target_entity = await get_input_entity(target_chat_id)

    if not source_entity or not target_entity:
        await event.respond('Invalid source or target chat. Please check the chat IDs.')
        return

    # Define the download directory path
    download_dir = '/root/bino'  # Change this to your desired directory

    # Create the download directory if it doesn't exist
    os.makedirs(download_dir, exist_ok=True)

    # Download and forward messages
    async def download_messages(source_entity, target_entity, download_dir, limit, start_id):
        message_count = 0
        async for message in client.iter_messages(source_entity, limit=limit, offset_id=start_id, reverse=True) if start_id else client.iter_messages(source_entity, limit=limit):
            message_count += 1

        with tqdm(total=message_count, desc='Cloning Messages', unit='message') as pbar:
            async for message in client.iter_messages(source_entity, limit=limit, offset_id=start_id, reverse=True) if start_id else client.iter_messages(source_entity, limit=limit):
                # Prepare caption
                caption = message.text if message.text else None

                # Function to download and forward media with renewed file reference if needed
                async def download_and_forward_media(message, target_entity, caption):
                    retry = True
                    while retry:
                        try:
                            media_path = await message.download_media(download_dir, progress_callback=lambda d, t: pbar.set_postfix_str(f'Downloaded: {d}/{t} bytes'))
                            retry = False  # Exit the loop if download is successful
                        except FileReferenceExpiredError:
                            # Renew the file reference and retry downloading
                            message = await client.get_messages(source_entity, ids=message.id)
                        except Exception as e:
                            logger.error(f"Failed to download media: {e}")
                            retry = False  # Exit the loop if other errors occur

                    if media_path and os.path.exists(media_path):
                        if isinstance(message.media, types.MessageMediaDocument):
                            # Forward document files (including videos)
                            await client.send_file(
                                target_entity,
                                media_path,
                                caption=caption,
                                supports_streaming=True,  # Make video streamable
                                force_document=False,  # Send video as video
                                thumb=await message.download_media(thumb=-1) if isinstance(message.media, types.MessageMediaDocument) else None,  # Add thumbnail
                                progress_callback=lambda d, t: pbar.set_postfix_str(f'Uploaded: {d}/{t} bytes')
                            )
                        else:
                            # Forward other files (documents, etc.)
                            await client.send_file(
                                target_entity,
                                media_path,
                                caption=caption,
                                thumb=await message.download_media(thumb=-1) if isinstance(message.media, types.MessageMediaDocument) else None,  # Add thumbnail
                                progress_callback=lambda d, t: pbar.set_postfix_str(f'Uploaded: {d}/{t} bytes')
                            )
                        # Delete the message file after forwarding
                        os.remove(media_path)
                    elif caption:
                        await client.send_message(target_entity, caption)

                # Download media files and forward them
                if message.media:
                    await download_and_forward_media(message, target_entity, caption)
                else:
                    # If there's no media, send the text message with caption
                    if caption:
                        await client.send_message(target_entity, caption)

                # Update progress bar
                pbar.update(1)
                # Show progress of message being cloned
                await event.respond(f'🌘 Clone success post id {message.id} -> {target_chat_id} '
                                    f'in {target_entity} (show message being cloned at the moment)')
                # Introduce a 4-second delay between message operations
                await asyncio.sleep(4)

    # Handle media groups (albums)
    async def handle_media_group(messages, target_entity):
        media_paths = []
        for message in messages:
            caption = message.text if message.text else None
            retry = True
            while retry:
                try:
                    media_path = await message.download_media(download_dir)
                    media_paths.append(media_path)
                    retry = False  # Exit the loop if download is successful
                except FileReferenceExpiredError:
                    # Renew the file reference and retry downloading
                    message = await client.get_messages(source_entity, ids=message.id)
                except Exception as e:
                    logger.error(f"Failed to download media for message ID: {message.id}")
                    retry = False  # Exit the loop if other errors occur

        # Forward the media group
        if media_paths:
            await client.send_file(
                target_entity,
                media_paths,
                caption=caption
            )
            # Delete the message files after forwarding
            for path in media_paths:
                os.remove(path)

    await download_messages(source_entity, target_entity, download_dir, limit, start_id)

    logger.info('Clone task completed for user: %s', event.sender_id)
    await event.respond('Messages cloned and forwarded successfully!')

# Create a scheduler
scheduler = AsyncIOScheduler()

# Schedule the account connection task to run at startup
scheduler.add_job(connect_account, 'date')

# Start the scheduler
scheduler.start()

# Start the bot
try:
    client.start()  # Start the client
    client.run_until_disconnected()
finally:
    scheduler.shutdown()
