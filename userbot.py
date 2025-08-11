#!/usr/bin/env python3
"""
UserBot для автоматического мониторинга Telegram каналов
Автоматически пересылает все посты из указанных каналов основному боту
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Set

from telethon import TelegramClient, events
from telethon.tl.types import Channel, Message, MessageMediaPhoto, MessageMediaDocument
from telethon.errors import FloodWaitError, ChatAdminRequiredError, ChannelPrivateError

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

class ChannelMonitor:
    def __init__(self, api_id: int, api_hash: str, phone: str, bot_token: str):
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone
        self.bot_token = bot_token
        
        # Инициализация клиентов
        self.userbot = TelegramClient('userbot_session', api_id, api_hash)
        self.bot = TelegramClient('bot_session', api_id, api_hash).start(bot_token=bot_token)
        
        # Загружаем настройки
        self.settings = self.load_settings()
        self.source_channels: Set[int] = set(self.settings.get("source_channels", []))
        self.bot_chat_id = self.settings.get("bot_chat_id")
        
        # Кэш для отслеживания уже обработанных сообщений
        self.processed_messages: Set[str] = set()
        
        # Буфер для медиа-групп
        self.media_buffer: Dict[str, List[Message]] = {}
        
        logger.info(f"UserBot инициализирован. Мониторинг {len(self.source_channels)} каналов")
    
    def load_settings(self) -> Dict:
        """Загружает настройки из файла"""
        try:
            with open('userbot_settings.json', 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            # Создаем файл с настройками по умолчанию
            default_settings = {
                "source_channels": [],
                "bot_chat_id": None,
                "auto_forward": True,
                "include_media": True,
                "forward_delay": 0
            }
            self.save_settings(default_settings)
            return default_settings
    
    def save_settings(self, settings: Dict = None) -> None:
        """Сохраняет настройки в файл"""
        if settings is None:
            settings = self.settings
        with open('userbot_settings.json', 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
    
    async def start(self):
        """Запускает UserBot"""
        try:
            # Запускаем UserBot
            await self.userbot.start(phone=self.phone)
            logger.info("UserBot успешно запущен")
            
            # Регистрируем обработчики событий
            self.register_handlers()
            
            # Запускаем мониторинг
            await self.monitor_channels()
            
        except Exception as e:
            logger.error(f"Ошибка запуска UserBot: {e}")
            raise
    
    def register_handlers(self):
        """Регистрирует обработчики событий"""
        
        @self.userbot.on(events.NewMessage(chats=self.source_channels))
        async def handle_new_message(event: events.NewMessage.Event):
            """Обрабатывает новые сообщения из мониторимых каналов"""
            try:
                message: Message = event.message
                chat_id = event.chat_id
                
                # Проверяем, не обрабатывали ли мы уже это сообщение
                message_key = f"{chat_id}:{message.id}"
                if message_key in self.processed_messages:
                    return
                
                logger.info(f"Новое сообщение из канала {chat_id}:{message.id}")
                
                # Обрабатываем медиа-группы
                if message.media_group_id:
                    await self.handle_media_group(message)
                else:
                    # Одиночное сообщение
                    await self.forward_message(message, chat_id)
                
                # Отмечаем как обработанное
                self.processed_messages.add(message_key)
                
                # Очищаем старые записи (оставляем последние 1000)
                if len(self.processed_messages) > 1000:
                    self.processed_messages.clear()
                
            except Exception as e:
                logger.error(f"Ошибка обработки сообщения: {e}")
        
        @self.userbot.on(events.MessageEdited(chats=self.source_channels))
        async def handle_edited_message(event: events.MessageEdited.Event):
            """Обрабатывает отредактированные сообщения"""
            try:
                message: Message = event.message
                chat_id = event.chat_id
                
                # Проверяем, не обрабатывали ли мы уже это сообщение
                message_key = f"{chat_id}:{message.id}"
                if message_key in self.processed_messages:
                    return
                
                logger.info(f"Отредактированное сообщение из канала {chat_id}:{message.id}")
                
                # Обрабатываем как новое сообщение
                if message.media_group_id:
                    await self.handle_media_group(message)
                else:
                    await self.forward_message(message, chat_id)
                
                self.processed_messages.add(message_key)
                
            except Exception as e:
                logger.error(f"Ошибка обработки отредактированного сообщения: {e}")
    
    async def handle_media_group(self, message: Message):
        """Обрабатывает медиа-группы"""
        media_group_id = message.media_group_id
        chat_id = message.chat_id
        
        # Добавляем сообщение в буфер
        if media_group_id not in self.media_buffer:
            self.media_buffer[media_group_id] = []
        
        self.media_buffer[media_group_id].append(message)
        
        # Ждем немного, чтобы собрать все части медиа-группы
        await asyncio.sleep(2)
        
        # Проверяем, собрались ли все части
        if len(self.media_buffer[media_group_id]) >= 1:  # Минимум 1 сообщение
            await self.forward_media_group(media_group_id, chat_id)
            
            # Очищаем буфер
            del self.media_buffer[media_group_id]
    
    async def forward_media_group(self, media_group_id: str, chat_id: int):
        """Пересылает медиа-группу боту"""
        try:
            messages = self.media_buffer.get(media_group_id, [])
            if not messages:
                return
            
            # Сортируем по ID сообщения для правильного порядка
            messages.sort(key=lambda m: m.id)
            
            # Пересылаем все сообщения из группы
            for message in messages:
                await self.forward_message(message, chat_id)
                await asyncio.sleep(0.5)  # Небольшая задержка между пересылками
            
            logger.info(f"Переслана медиа-группа {media_group_id} из канала {chat_id}")
            
        except Exception as e:
            logger.error(f"Ошибка пересылки медиа-группы: {e}")
    
    async def forward_message(self, message: Message, source_chat_id: int):
        """Пересылает сообщение боту"""
        try:
            if not self.bot_chat_id:
                logger.warning("bot_chat_id не установлен, пропускаем пересылку")
                return
            
            # Создаем текст для пересылки
            forward_text = f"📢 Новость из канала {source_chat_id}\n"
            forward_text += f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
            forward_text += f"📝 ID: {source_chat_id}:{message.id}"
            
            if message.media_group_id:
                forward_text += f"\n🖼 Медиа-группа: {message.media_group_id}"
            
            # Пересылаем сообщение боту
            await self.userbot.forward_messages(
                entity=self.bot_chat_id,
                messages=message
            )
            
            # Отправляем дополнительную информацию
            await self.userbot.send_message(
                entity=self.bot_chat_id,
                message=forward_text
            )
            
            logger.info(f"Сообщение {message.id} переслано боту")
            
        except Exception as e:
            logger.error(f"Ошибка пересылки сообщения: {e}")
    
    async def monitor_channels(self):
        """Основной цикл мониторинга"""
        logger.info("Запущен мониторинг каналов")
        
        try:
            while True:
                # Проверяем каналы каждые 30 секунд
                await asyncio.sleep(30)
                
                # Проверяем доступность каналов
                await self.check_channels_access()
                
        except KeyboardInterrupt:
            logger.info("Получен сигнал остановки")
        except Exception as e:
            logger.error(f"Ошибка в цикле мониторинга: {e}")
        finally:
            await self.stop()
    
    async def check_channels_access(self):
        """Проверяет доступность мониторимых каналов"""
        for chat_id in list(self.source_channels):
            try:
                chat = await self.userbot.get_entity(chat_id)
                if not chat:
                    logger.warning(f"Канал {chat_id} недоступен, удаляем из списка")
                    self.source_channels.remove(chat_id)
                    self.settings["source_channels"] = list(self.source_channels)
                    self.save_settings()
                    
            except (ValueError, ChannelPrivateError, ChatAdminRequiredError):
                logger.warning(f"Канал {chat_id} недоступен, удаляем из списка")
                self.source_channels.remove(chat_id)
                self.settings["source_channels"] = list(self.source_channels)
                self.save_settings()
                
            except Exception as e:
                logger.error(f"Ошибка проверки канала {chat_id}: {e}")
    
    async def add_source_channel(self, chat_id: int) -> bool:
        """Добавляет канал в список для мониторинга"""
        try:
            # Проверяем доступность канала
            chat = await self.userbot.get_entity(chat_id)
            if not chat:
                return False
            
            if chat_id not in self.source_channels:
                self.source_channels.add(chat_id)
                self.settings["source_channels"] = list(self.source_channels)
                self.save_settings()
                logger.info(f"Добавлен канал для мониторинга: {chat_id}")
                return True
            else:
                logger.info(f"Канал {chat_id} уже в списке мониторинга")
                return True
                
        except Exception as e:
            logger.error(f"Ошибка добавления канала {chat_id}: {e}")
            return False
    
    async def remove_source_channel(self, chat_id: int) -> bool:
        """Удаляет канал из списка мониторинга"""
        try:
            if chat_id in self.source_channels:
                self.source_channels.remove(chat_id)
                self.settings["source_channels"] = list(self.source_channels)
                self.save_settings()
                logger.info(f"Удален канал из мониторинга: {chat_id}")
                return True
            else:
                logger.info(f"Канал {chat_id} не найден в списке мониторинга")
                return False
                
        except Exception as e:
            logger.error(f"Ошибка удаления канала {chat_id}: {e}")
            return False
    
    async def set_bot_chat_id(self, chat_id: int) -> bool:
        """Устанавливает ID чата бота для пересылки"""
        try:
            self.bot_chat_id = chat_id
            self.settings["bot_chat_id"] = chat_id
            self.save_settings()
            logger.info(f"Установлен ID чата бота: {chat_id}")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка установки ID чата бота: {e}")
            return False
    
    async def stop(self):
        """Останавливает UserBot"""
        try:
            await self.userbot.disconnect()
            await self.bot.disconnect()
            logger.info("UserBot остановлен")
        except Exception as e:
            logger.error(f"Ошибка остановки UserBot: {e}")


async def main():
    """Главная функция"""
    # Загружаем конфигурацию
    try:
        from userbot_config import API_ID, API_HASH, PHONE, BOT_TOKEN
    except ImportError as e:
        logger.error(f"Ошибка импорта конфигурации: {e}")
        logger.info("Проверьте файл userbot_config.py и убедитесь, что все переменные определены")
        return
    except FileNotFoundError:
        logger.error("Файл userbot_config.py не найден!")
        logger.info("Создайте файл userbot_config.py с настройками:")
        logger.info("API_ID = ваш_api_id")
        logger.info("API_HASH = 'ваш_api_hash'")
        logger.info("PHONE = 'ваш_номер_телефона'")
        logger.info("BOT_TOKEN = 'токен_вашего_бота'")
        return
    
    try:
        # Создаем и запускаем UserBot
        monitor = ChannelMonitor(API_ID, API_HASH, PHONE, BOT_TOKEN)
        await monitor.start()
        
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        logger.info("Проверьте настройки в userbot_config.py")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Программа остановлена пользователем")
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {e}")
