/* Russian strings used by the interface and external catalogue metadata. */
(() => {
  "use strict";

  const messages = {
    "app.name": "Курымдык",
    "nav.library": "Библиотека",
    "nav.wave": "Моя волна",
    "nav.weekly": "Подборка недели",
    "nav.artists": "Исполнители",
    "nav.stats": "Статистика",
    "nav.profile": "Профиль исполнителя",
    "nav.quality": "Качество библиотеки",
    "action.open": "Открыть",
    "action.close": "Закрыть",
    "action.cancel": "Отмена",
    "action.save": "Сохранить",
    "action.refresh": "Обновить",
    "action.search": "Поиск",
    "action.download": "Скачать",
    "action.play": "Играть",
    "action.pause": "Пауза",
    "action.previous": "Предыдущий",
    "action.next": "Следующий",
    "state.loading": "Загрузка...",
    "state.error": "Ошибка",
    "state.notAvailable": "Данные недоступны",
    "search.placeholder": "Поиск по библиотеке…",
    "search.tracks": "Треки",
    "search.artists": "Исполнители",
    "search.albums": "Альбомы",
    "profile.readMore": "Читать дальше на Last.fm",
    "profile.openLastfm": "Открыть на Last.fm",
    "profile.listeners": "прослушиваний",
    "profile.unavailable": "Данные об артисте недоступны. Проверьте VPN или интернет.",
    "profile.noBiography": "Биография не указана.",
    "profile.inLibrary": "В библиотеке",
    "profile.download": "Скачать",
    "wave.new": "Новое",
    "wave.library": "Из моей",
    "wave.forgotten": "Забытое",
    "wave.favorite": "Любимое",
    "wave.mix": "Всё вместе",
    "wave.settings": "Настроить",
    "wave.mood": "Настроение",
    "wave.language": "Язык",
    "wave.balance": "Знакомое — новое",
    "theme.dark": "Тёмная",
    "theme.light": "Светлая",
    "theme.auto": "Автоматически",
    "theme.accent": "Цвет акцента",
    "theme.opacity": "Прозрачность интерфейса",
    "release.empty": "Новых релизов за выбранный период нет",
    "lyrics.unsynced": "Текст без синхронизации",
    "lyrics.notFound": "Текст песни не найден",
    "lyrics.failed": "Не удалось загрузить текст песни",
    "mode.clipLyrics": "Клип и текст",
    "mode.clipOnly": "Только клип",
    "mode.coverLyrics": "Обложка и текст",
    "mode.coverOnly": "Только обложка",
    "mode.lyricsOnly": "Только текст",
  };

  const genres = new Map(Object.entries({
    "Belarusian": "Белорусская музыка",
    "Hip-Hop": "Хип-хоп",
    "Hip Hop": "Хип-хоп",
    "Russian": "Русская музыка",
    "Underground HH": "Андеграунд хип-хоп",
    "Abstract Hip-Hop": "Абстрактный хип-хоп",
    "Russian Rap": "Русский рэп",
    "Electronic": "Электроника",
    "Pop": "Поп",
    "Rock": "Рок",
    "Indie": "Инди",
    "Metal": "Метал",
    "Folk": "Фолк",
    "Jazz": "Джаз",
    "Funk": "Фанк",
    "Ambient": "Эмбиент",
    "Punk": "Панк",
    "Alternative": "Альтернатива",
    "Alternative Rock": "Альтернативный рок",
    "Classical": "Классика",
    "Country": "Кантри",
    "Dance": "Танцевальная музыка",
    "Disco": "Диско",
    "Drum and Bass": "Драм-н-бейс",
    "House": "Хаус",
    "Techno": "Техно",
    "Trance": "Транс",
    "Soul": "Соул",
    "Blues": "Блюз",
    "Reggae": "Регги",
    "Soundtrack": "Саундтрек",
    "Singer-Songwriter": "Авторская песня",
  }));

  function t(key, params) {
    const template = messages[key] || key;
    return String(template).replace(/\{(\w+)\}/g, (_, name) =>
      params && params[name] !== undefined ? String(params[name]) : `{${name}}`);
  }

  function genre(value) {
    const raw = String(value || "").trim();
    if (!raw) return "";
    if (genres.has(raw)) return genres.get(raw);
    const known = [...genres.keys()].find((key) => key.toLowerCase() === raw.toLowerCase());
    if (known) return genres.get(known);
    return transliterateGenre(raw);
  }

  function transliterateGenre(value) {
    const map = {
      a: "а", b: "б", c: "к", d: "д", e: "э", f: "ф", g: "г", h: "х",
      i: "и", j: "дж", k: "к", l: "л", m: "м", n: "н", o: "о", p: "п",
      q: "к", r: "р", s: "с", t: "т", u: "у", v: "в", w: "в", x: "кс",
      y: "й", z: "з",
    };
    return value.replace(/[A-Za-z]+/g, (word) =>
      [...word].map((char) => {
        const translated = map[char.toLowerCase()] || char;
        return char === char.toUpperCase() ? translated.toUpperCase() : translated;
      }).join(""));
  }

  function date(value, options) {
    const parsed = value instanceof Date ? value : new Date(value);
    return Number.isNaN(parsed.getTime())
      ? String(value || "")
      : new Intl.DateTimeFormat("ru-RU", options || { day: "numeric", month: "short" }).format(parsed)
        .replace(/\s?г\.$/, "");
  }

  function localizeText(value) {
    return String(value || "")
      .replace(/\bRead more on Last\.fm\b/gi, t("profile.readMore"))
      .replace(/\bOpen on Last\.fm\b/gi, t("profile.openLastfm"))
      .replace(/\bListeners\b/gi, t("profile.listeners"))
      .replace(/\bLoading(?:\.\.\.)?\b/gi, t("state.loading"))
      .replace(/\bError\b/gi, t("state.error"));
  }

  window.i18n = Object.freeze({ t, genre, date, localizeText });
})();
