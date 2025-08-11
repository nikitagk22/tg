#!/usr/bin/env python3
"""
Скрипт для одновременного запуска основного бота и UserBot
"""

import asyncio
import subprocess
import sys
import time
import signal
import os

def start_process(command, name):
    """Запускает процесс и возвращает объект Process"""
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )
        print(f"✅ {name} запущен (PID: {process.pid})")
        return process
    except Exception as e:
        print(f"❌ Ошибка запуска {name}: {e}")
        return None

def monitor_process(process, name):
    """Мониторит процесс и выводит его логи"""
    if not process:
        return
    
    try:
        for line in iter(process.stdout.readline, ''):
            if line:
                print(f"[{name}] {line.rstrip()}")
    except Exception as e:
        print(f"❌ Ошибка мониторинга {name}: {e}")

def stop_process(process, name):
    """Останавливает процесс"""
    if not process:
        return
    
    try:
        process.terminate()
        process.wait(timeout=5)
        print(f"✅ {name} остановлен")
    except subprocess.TimeoutExpired:
        process.kill()
        print(f"⚠️ {name} принудительно остановлен")
    except Exception as e:
        print(f"❌ Ошибка остановки {name}: {e}")

def signal_handler(signum, frame):
    """Обработчик сигналов для корректного завершения"""
    print("\n🛑 Получен сигнал остановки, завершаем работу...")
    sys.exit(0)

async def main():
    """Главная функция"""
    print("🚀 Запуск системы мониторинга Telegram каналов")
    print("=" * 50)
    
    # Проверяем наличие необходимых файлов
    required_files = ['bot.py', 'userbot.py', 'userbot_config.py']
    missing_files = [f for f in required_files if not os.path.exists(f)]
    
    if missing_files:
        print(f"❌ Отсутствуют необходимые файлы: {', '.join(missing_files)}")
        print("Убедитесь, что все файлы находятся в текущей папке")
        return
    
    # Регистрируем обработчик сигналов
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    processes = []
    
    try:
        # Запускаем UserBot
        print("\n📡 Запуск UserBot...")
        userbot_process = start_process([sys.executable, 'userbot.py'], 'UserBot')
        if userbot_process:
            processes.append(('UserBot', userbot_process))
        
        # Ждем немного для инициализации UserBot
        await asyncio.sleep(3)
        
        # Запускаем основной бот
        print("\n🤖 Запуск основного бота...")
        bot_process = start_process([sys.executable, 'bot.py'], 'Основной бот')
        if bot_process:
            processes.append(('Основной бот', bot_process))
        
        if not processes:
            print("❌ Не удалось запустить ни одного процесса")
            return
        
        print(f"\n✅ Запущено процессов: {len(processes)}")
        print("=" * 50)
        print("📊 Логи процессов:")
        print("=" * 50)
        
        # Запускаем мониторинг всех процессов
        tasks = []
        for name, process in processes:
            task = asyncio.create_task(
                asyncio.to_thread(monitor_process, process, name)
            )
            tasks.append(task)
        
        # Ждем завершения всех процессов
        await asyncio.gather(*tasks, return_exceptions=True)
        
    except KeyboardInterrupt:
        print("\n🛑 Получен сигнал остановки")
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
    finally:
        # Останавливаем все процессы
        print("\n🛑 Остановка процессов...")
        for name, process in processes:
            stop_process(process, name)
        
        print("\n✅ Все процессы остановлены")
        print("👋 До свидания!")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Программа остановлена пользователем")
    except Exception as e:
        print(f"\n❌ Неожиданная ошибка: {e}")
        sys.exit(1)
