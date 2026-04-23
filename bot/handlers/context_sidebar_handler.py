import logging
import math
import html
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler, ConversationHandler
from storage import storage_manager
import config

logger = logging.getLogger(__name__)

# Callback data prefixes
CTX_PREFIX = "ctx_"
CTX_NAV = f"{CTX_PREFIX}nav_"    # ctx_nav_<page>
CTX_DEL = f"{CTX_PREFIX}del_"    # ctx_del_<page>_<hash_or_id?>
CTX_CONFIRM = f"{CTX_PREFIX}cfm_" # ctx_cfm_<page>_<start_pk>
CTX_RESEND = f"{CTX_PREFIX}res_"  # ctx_res_<page>_<start_pk>
CTX_PANEL = f"{CTX_PREFIX}pnl_"   # ctx_pnl_<page>_<start_pk>
CTX_CLOSE = f"{CTX_PREFIX}close"

async def context_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point for /context command."""
    await show_context_page(update, context, page=0)

async def show_context_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    """Fetches history, groups it, and renders the specific page."""
    chat_id = update.effective_chat.id
    
    # 1. Fetch History with PKs
    raw_history = await storage_manager.get_thread_history_with_pk(chat_id, limit=200) # 200 msg limit for management seems reasonable
    
    if not raw_history:
        text = "📭 **Context is Empty**\n\nNo active history found explicitly for this thread."
        await _respond(update, text, close_button=True)
        return

    # 2. Group into Blocks (User + Assistant)
    # Strategy: Iterate backwards. Find Assistant msgs, then the preceding User msg. 
    # Or just iterate forwards and start a block on 'user'.
    blocks = []
    current_block = []
    
    # We want latest blocks first? Usually UI shows latest.
    # History is usually returned Oldest -> Newest by get_thread_history (based on SQL 'ORDER BY message_pk ASC')
    # So raw_history[0] is oldest.
    
    for msg in raw_history:
        if msg['role'] == 'user':
            if current_block:
                blocks.append(current_block)
            current_block = [msg]
        else:
            current_block.append(msg)
    if current_block:
        blocks.append(current_block)
        
    # Reverse blocks to show Newest first
    blocks.reverse()
    
    total_blocks = len(blocks)
    if total_blocks == 0:
         # Should be covered by empty history check, but failsafe
        text = "📭 **Context is Empty**\n\nNo interactions found."
        await _respond(update, text, close_button=True)
        return

    # Circular Pagination Logic
    if total_blocks > 0:
        page = page % total_blocks
    
    block = blocks[page]
    
    # Calculate Token Usage of this block
    block_text = "".join([m['content'] for m in block])
    # Heuristic is fine for speed, but user requested accuracy.
    # Let's try to use tiktoken if available, else fallback.
    try:
        import tiktoken
        encoding = tiktoken.get_encoding("cl100k_base")
        block_tokens = len(encoding.encode(block_text))
    except Exception:
        block_tokens = len(block_text) // 4
    
    # Render Block
    user_msg = next((m for m in block if m['role'] == 'user'), None)
    
    # Identify the Block Key (Start PK) for deletion
    # If a block has no user message (orphan assistant), use the first message's pk
    start_pk = block[0]['id']
    
    # Global calc - Iterate all blocks
    total_tokens = 0
    try:
        import tiktoken
        encoding = tiktoken.get_encoding("cl100k_base")
        for b in blocks:
            for m in b:
                total_tokens += len(encoding.encode(m['content']))
    except Exception:
        # Fallback to heuristic
        total_tokens = sum(len(m['content'])//4 for b in blocks for m in b)
        
    text = f"🧠 <b>Context Manager</b> (Interaction {page + 1}/{total_blocks})\n"
    text += f"Total Context: ~{total_tokens} tokens\n"
    text += "──────────────────\n"
    
    if user_msg:
        safe_user_content = html.escape(user_msg['content'][:150])
        text += f"👤 <b>User</b>: {safe_user_content}...\n\n"
    else:
        text += f"👤 <b>User</b>: [Missing/System Prompt]\n\n"
        
    asst_count = len([m for m in block if m['role'] != 'user'])
    text += f"🤖 <b>Assistant</b>: ({asst_count} messages, ~{block_tokens} tokens)\n"
    
    # Show snippet of last message
    if block[-1]['role'] != 'user':
        safe_last_content = html.escape(block[-1]['content'][:100])
        text += f"<i>{safe_last_content}...</i>\n"
        
    text += "──────────────────"
    
    # Keyboard
    buttons = []
    
    # Nav Row
    if total_blocks > 1:
        prev_page = (page - 1) % total_blocks
        next_page = (page + 1) % total_blocks
        nav_row = [
            InlineKeyboardButton("⬅️ Newer", callback_data=f"{CTX_NAV}{prev_page}"),
            InlineKeyboardButton("Older ➡️", callback_data=f"{CTX_NAV}{next_page}")
        ]
        buttons.append(nav_row)
    
    # Action Row
    buttons.append([
        InlineKeyboardButton("🗑️ Delete this Interaction", callback_data=f"{CTX_CONFIRM}{page}_{start_pk}"),
        InlineKeyboardButton("💬 Resume as Panel", callback_data=f"{CTX_PANEL}{page}_{start_pk}")
    ])
    
    if asst_count > 0:
        buttons.append([
            InlineKeyboardButton("📤 Resend Assistant Reply", callback_data=f"{CTX_RESEND}{page}_{start_pk}")
        ])
    
    buttons.append([InlineKeyboardButton("Done", callback_data=CTX_CLOSE)])
    
    keyboard = InlineKeyboardMarkup(buttons)
    
    await _respond(update, text, keyboard)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    
    # CTX_PANEL callbacks are handled by the discuss_panel_conv_handler entry point.
    # Do NOT answer or consume them here — just pass through.
    if data.startswith(CTX_PANEL):
        return
    
    await query.answer()
    
    if data == CTX_CLOSE:
        await query.message.delete()
        return
        
    if data.startswith(CTX_NAV):
        page = int(data.split("_")[-1])
        await show_context_page(update, context, page)
        return
        
    if data.startswith(CTX_CONFIRM):
        # Format: ctx_cfm_<page>_<start_pk>
        _, _, page_str, start_pk_str = data.split("_")
        page = int(page_str)
        
        # Show confirmation
        buttons = [
            [
                InlineKeyboardButton("❌ Cancel", callback_data=f"{CTX_NAV}{page}"),
                InlineKeyboardButton("✅ Yes, Delete", callback_data=f"{CTX_DEL}{page}_{start_pk_str}")
            ]
        ]
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith(CTX_RESEND):
        # Format: ctx_res_<page>_<start_pk>
        _, _, page_str, start_pk_str = data.split("_")
        start_pk = int(start_pk_str)
        chat_id = update.effective_chat.id
        
        raw_history = await storage_manager.get_thread_history_with_pk(chat_id, limit=200)
        
        # Group to find the assistant messages in this block
        assistant_contents = []
        found_start = False
        
        for msg in raw_history:
            if msg['id'] == start_pk:
                found_start = True
                if msg['role'] != 'user':
                    assistant_contents.append(msg['content'])
                continue
            
            if found_start:
                if msg['role'] == 'user':
                    break
                assistant_contents.append(msg['content'])
                
        if assistant_contents:
            joined_response = "\n\n".join(assistant_contents)
            await query.answer("Resending...", show_alert=False)
            from bot.messaging import send_safe_message
            await send_safe_message(context, update, joined_response)
        else:
            await query.answer("No AI response found in this interaction to resend.", show_alert=True)
        return

    if data.startswith(CTX_DEL):
        # Format: ctx_del_<page>_<start_pk>
        _, _, page_str, start_pk_str = data.split("_")
        page = int(page_str)
        start_pk = int(start_pk_str)
        chat_id = update.effective_chat.id
        
        # Perform Deletion
        # Logic: We need to recreate the block to know WHICH IDs to delete.
        # This assumes the history hasn't shifted drastically. 
        # A safer way relies on the start_pk.
        
        raw_history = await storage_manager.get_thread_history_with_pk(chat_id, limit=200)
        
        # Re-group to find the block starting with start_pk
        ids_to_delete = []
        found_block = False
        
        # We need strict grouping logic again
        # Actually, if we just look for start_pk, and then take all subsequent msg IDs ensuring they are NOT user roles (unless it's the start_pk itself)
        # Wait, the block includes the User message (start_pk) AND subsequent assistant messages.
        
        # Let's iterate raw_history (Old -> New)
        # Find start_pk.
        # Add start_pk to delete list.
        # Continue adding subsequent messages IF they are NOT 'user'.
        # Stop at next 'user' or end of list.
        
        found_start = False
        for msg in raw_history:
            if msg['id'] == start_pk:
                found_start = True
                ids_to_delete.append(msg['id'])
                continue
            
            if found_start:
                if msg['role'] == 'user':
                    break # End of this block
                ids_to_delete.append(msg['id'])
        
        if ids_to_delete:
            await storage_manager.delete_messages(chat_id, ids_to_delete)
            await query.answer(f"Deleted {len(ids_to_delete)} messages.", show_alert=True)
            # Refresh page (stay on same index if possible, else shift)
            # If we delete page 0, new page 0 is the next one.
            await show_context_page(update, context, page) 
        else:
            await query.answer("Error: faster update occurred? Block not found.", show_alert=True)
            await show_context_page(update, context, page)


async def _respond(update: Update, text: str, keyboard=None, close_button=False):
    if close_button and not keyboard:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Close", callback_data=CTX_CLOSE)]])
        
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode=constants.ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode=constants.ParseMode.HTML)

context_sidebar_handler = CommandHandler("context", context_command)
context_callback_handler = CallbackQueryHandler(handle_callback, pattern=f"^{CTX_PREFIX}")
