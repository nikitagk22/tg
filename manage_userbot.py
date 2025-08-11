#!/usr/bin/env python3
"""
Скрипт управления UserBot для мониторинга каналов
Позволяет добавлять/удалять каналы и настраивать параметры
"""

import asyncio
import json
import sys
from typing import Dict, List

from telethon import TelegramClient
from telethon.errors import FloodWaitError, ChatAdminRequiredError, ChannelPrivateError

class UserBotManager:
    def __init__(self, api_id: int, api_hash: str, phone: str):
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone
        self.client = TelegramClient('userbot_session', api_id, api_hash)
        self.settings_file = 'userbot_settings.json'
    
    async def start(self):
        """Запускает клиент"""
        await self.client.start(phone=self.phone)
        print("✅ UserBot клиент запущен")
    
    async def stop(self):
        """Останавливает клиент"""
        await self.client.disconnect()
        print("✅ UserBot клиент остановлен")
    
    def load_settings(self) -> Dict:
        """Загружает настройки"""
        try:
            with open(self.settings_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return {"source_channels": [], "bot_chat_id": None}
    
    def save_settings(self, settings: Dict):
        """Сохраняет настройки"""
        with open(self.settings_file, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
    
    async def resolve_channel(self, channel_input: str) -> int:
        """Разрешает канал по username или ID"""
        try:
            if channel_input.startswith('@'):
                # Username
                entity = await self.client.get_entity(channel_input)
            elif channel_input.startswith('-100'):
                # Channel ID
                entity = await self.client.get_entity(int(channel_input))
            else:
                # Попробуем как ID
                entity = await self.client.get_entity(int(channel_input))
            
            return entity.id
        except Exception as e:
            print(f"❌ Ошибка разрешения канала '{channel_input}': {e}")
            return None
    
    async def add_channel(self, channel_input: str) -> bool:
        """Добавляет канал в список мониторинга"""
        channel_id = await self.resolve_channel(channel_input)
        if not channel_id:
            return False
        
        settings = self.load_settings()
        if channel_id not in settings["source_channels"]:
            settings["source_channels"].append(channel_id)
            self.save_settings(settings)
            print(f"✅ Канал {channel_id} добавлен в мониторинг")
            return True
        else:
            print(f"ℹ️ Канал {channel_id} уже в списке мониторинга")
            return True
    
    async def remove_channel(self, channel_input: str) -> bool:
        """Удаляет канал из списка мониторинга"""
        channel_id = await self.resolve_channel(channel_input)
        if not channel_id:
            return False
        
        settings = self.load_settings()
        if channel_id in settings["source_channels"]:
            settings["source_channels"].remove(channel_id)
            self.save_settings(settings)
            print(f"✅ Канал {channel_id} удален из мониторинга")
            return True
        else:
            print(f"ℹ️ Канал {channel_id} не найден в списке мониторинга")
            return False
    
    async def list_channels(self):
        """Показывает список мониторимых каналов"""
        settings = self.load_settings()
        channels = settings["source_channels"]
        
        if not channels:
            print("📋 Список мониторимых каналов пуст")
            return
        
        print(f"📋 Мониторимые каналы ({len(channels)}):")
        for i, channel_id in enumerate(channels, 1):
            try:
                entity = await self.client.get_entity(channel_id)
                title = getattr(entity, 'title', 'Неизвестно')
                username = getattr(entity, 'username', None)
                username_str = f" (@{username})" if username else ""
                print(f"  {i}. {title} (ID: {channel_id}){username_str}")
            except Exception as e:
                print(f"  {i}. ID: {channel_id} (ошибка получения: {e})")
    
    async def set_bot_chat(self, chat_input: str) -> bool:
        """Устанавливает чат бота для пересылки"""
        try:
            if chat_input.startswith('@'):
                # Username бота
                entity = await self.client.get_entity(chat_input)
            else:
                # ID чата
                entity = await self.client.get_entity(int(chat_input))
            
            settings = self.load_settings()
            settings["bot_chat_id"] = entity.id
            self.save_settings(settings)
            print(f"✅ Установлен чат бота: {entity.id}")
            return True
            
        except Exception as e:
            print(f"❌ Ошибка установки чата бота: {e}")
            return False
    
    async def test_channel_access(self, channel_input: str):
        """Тестирует доступ к каналу"""
        channel_id = await self.resolve_channel(channel_input)
        if not channel_id:
            return
        
        try:
            entity = await self.client.get_entity(channel_id)
            title = getattr(entity, 'title', 'Неизвестно')
            username = getattr(entity, 'username', None)
            participants_count = getattr(entity, 'participants_count', 'Неизвестно')
            
            print(f"✅ Доступ к каналу '{title}' подтвержден")
            print(f"   ID: {channel_id}")
            if username:
                print(f"   Username: @{username}")
            print(f"   Участников: {participants_count}")
            
        except ChannelPrivateError:
            print(f"❌ Канал {channel_input} приватный, доступ ограничен")
        except ChatAdminRequiredError:
            print(f"❌ Канал {channel_input} требует права администратора")
        except Exception as e:
            print(f"❌ Ошибка доступа к каналу: {e}")
    
    async def show_status(self):
        """Показывает статус UserBot"""
        settings = self.load_settings()
        
        print("📊 Статус UserBot:")
        print(f"   Мониторимых каналов: {len(settings['source_channels'])}")
        print(f"   Чат бота: {settings.get('bot_chat_id', 'Не установлен')}")
        
        if settings["source_channels"]:
            print("\n📋 Каналы:")
            await self.list_channels()


async def main():
    """Главная функция"""
    # Загружаем конфигурацию
    try:
        with open('userbot_config.py', 'r', encoding='utf-8') as f:
            config_content = f.read()
            # Извлекаем значения из конфига
            exec(config_content)
    except FileNotFoundError:
        print("❌ Файл userbot_config.py не найден!")
        print("Создайте файл с настройками API_ID, API_HASH, PHONE")
        return
    
    if len(sys.argv) < 2:
        print("📖 Использование:")
        print("  python manage_userbot.py add <канал>     - добавить канал")
        print("  python manage_userbot.py remove <канал>  - удалить канал")
        print("  python manage_userbot.py list            - список каналов")
        print("  python manage_userbot.py set-bot <чат>   - установить чат бота")
        print("  python manage_userbot.py test <канал>    - проверить доступ")
        print("  python manage_userbot.py status          - статус")
        print("\nПримеры:")
        print("  python manage_userbot.py add @channel_name")
        print("  python manage_userbot.py add -1001234567890")
        print("  python manage_userbot.py set-bot @your_bot")
        return
    
    command = sys.argv[1].lower()
    
    try:
        manager = UserBotManager(API_ID, API_HASH, PHONE)
        await manager.start()
        
        if command == "add" and len(sys.argv) > 2:
            channel = sys.argv[2]
            await manager.add_channel(channel)
            
        elif command == "remove" and len(sys.argv) > 2:
            channel = sys.argv[2]
            await manager.remove_channel(channel)
            
        elif command == "list":
            await manager.list_channels()
            
        elif command == "set-bot" and len(sys.argv) > 2:
            chat = sys.argv[2]
            await manager.set_bot_chat(chat)
            
        elif command == "test" and len(sys.argv) > 2:
            channel = sys.argv[2]
            await manager.test_channel_access(channel)
            
        elif command == "status":
            await manager.show_status()
            
        else:
            print("❌ Неизвестная команда или недостаточно параметров")
            print("Используйте 'python manage_userbot.py' для справки")
        
        await manager.stop()
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")


if __name__ == "__main__":
    asyncio.run(main())
