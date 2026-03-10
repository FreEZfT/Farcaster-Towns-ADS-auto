НА СЕГОДНЯШНИЙ ДЕНЬ ПРОЕКТЫ НЕ АКТУАЛЬНЫ

Автоматизация и эмулирование реального пользователя в проектах Farcaster и Towns

Автоматизация происходит через ADS Power с использованием Selenium WebDriver

Что умеет софт: Farcaster: скроллинг ленты, лайки, комменты, репосты, создание рандомного поста с использованием подготовленных тексов и картинок, подписка на других пользователей

Towns: переключение между каналми, отправка сгенерированного текста с помощью api anthropic/claude-3-haiku, скроллинг ленты, ежедневный tap по "бобру" для получения поинтов

Настройки в config

Скачивание и установка:

git clone https://github.com/FreEZfT/Farcaster-Towns-ADS-auto.git

cd Warpcast_Towns

python -m venv .venv

.venv\Scripts\activate

pip install -r requirements.txt

python WARP_Town_Overlay.py

