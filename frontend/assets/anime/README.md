Положи сюда 15 обоев в формате: wallpaper-01.jpg ... wallpaper-15.jpg
Размер 2560×1440 или больше, JPG/PNG.

Инструкция:
- Папка: frontend/assets/anime/wallpapers/
- Формат: wallpaper-01.jpg, wallpaper-02.jpg, ..., wallpaper-15.jpg
- Размер: 2560×1440 или больше, JPG/PNG (sRGB, 80% качество, 1–2MB)
- Если файлов нет — используется градиент #0f0a1a → #1a0f2e + встроенные SVG-обои (data-URI)

Подробнее:
- Главный фон темы Anime: html[data-theme="anime"] body { background-image: var(--anime-wallpaper, linear-gradient(...)) }
- Встроенные фолбеки (3 штуки) — SVG-градиенты с силуэтами, работают без файлов
- Кнопка «Сменить обои» в настройках (⚙ → Сменить обои) переключает 1→15
- Автосмена раз в 10 минут
- Функция applyAnimeWallpaper(idx) проверяет fetch HEAD /static/assets/anime/wallpapers/wallpaper-NN.jpg и ставит --anime-wallpaper
