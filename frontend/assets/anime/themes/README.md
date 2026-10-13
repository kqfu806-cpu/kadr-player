# Аниме-темы — инструкция

Положи сюда картинки для 4 популярных аниме. Структура:

```
frontend/assets/anime/themes/
  attack-on-titan/
    dark/wallpaper.jpg
    light/wallpaper.jpg
    avatars/avatar-01.jpg ... avatar-08.jpg
  berserk/
    dark/wallpaper.jpg
    light/wallpaper.jpg
    avatars/avatar-01.jpg ... avatar-08.jpg
  overlord/
    dark/... light/... avatars/...
  future-diary/
    dark/... light/... avatars/...
```

## Что положить
- **wallpaper.jpg** — главный фон для темы. Тёмный и светлый — разные картинки (тёмный — мрачный, светлый — яркий). Размер 2560×1440 или больше, JPG/PNG, sRGB, 80% качество, 1–2 MB.
- **wallpaper-01.jpg ... wallpaper-15.jpg** (опционально) — дополнительные обои для кнопки «Сменить обои». Если файлов нет — fallback на градиент.
- **avatars/avatar-01.jpg ... 08.jpg** — аватарки персонажей (Канкэ, Микаса, Гатс, Айнз, Юно и т.д.) для профиля. 256×256 или 512×512, квадрат, JPG.
- Если файлов нет — используется градиент + встроенные SVG-обои (data-URI), как в теме anime.

## Как работает
- В `styles.css` созданы темы:
  `html[data-theme="anime-titan-dark"]` → `--anime-wallpaper: url('/static/assets/anime/themes/attack-on-titan/dark/wallpaper.jpg'); --accent: #dc2626;`
  Аналогично для `anime-titan-light`, `anime-berserk-dark/light`, `anime-overlord-dark/light`, `anime-diary-dark/light`.
- Каждая тема наследует `body { background-image: var(--anime-wallpaper, var(--anime-gradient)); }`
- В настройках (`⚙ → Аниме-темы`) сетка 8 кнопок `data-anime-theme="anime-titan-dark"` и т.д.
- Клик → `applyAnimeTheme(preset)` ставит `data-theme`, сохраняет `localStorage['kadr-anime-theme']`, при загрузке применяется.
- `.theme-card` — превью акцента (миниатюра), неоновая подсветка выбранной `is-active`.

## Смена обоев
- Кнопка «Сменить обои» (`#btnChangeWallpaper`) в аниме-теме меняет `wallpaper.jpg` на следующий `wallpaper-NN.jpg` из той же папки (01..15) через `fetch HEAD`.
- Если файлов нет — fallback на градиент, toast «Обои: файл не найден, показан градиент».

## Светлый / тёмный
- `*-dark` — тёмный фон #0a0a0c, акценты насыщенные (красный #dc2626, пурпурный #a855f7, розовый #f43f5e).
- `*-light` — светлый фон #f5f5f7, акценты осветлённые (#ef4444, #c084fc, #fb7185) и тёмный текст #1a1a1a.

## Примеры
```
themes/attack-on-titan/dark/wallpaper.jpg  — Стена Мария, титаны, мрачно
themes/attack-on-titan/light/wallpaper.jpg — Эрен в полёте, светлый
themes/berserk/dark/wallpaper.jpg           — Гатс с мечом, ночь
themes/overlord/light/wallpaper.jpg         — Айнз на троне, светлый
```
