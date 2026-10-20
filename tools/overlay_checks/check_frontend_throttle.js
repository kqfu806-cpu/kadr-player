/* Проверка троттлинга публикации состояния во фронтенде.
 *
 * Запуск:  node tools/overlay_checks/check_frontend_throttle.js
 *
 * Что проверяем:
 *   1) блок троттлинга вообще есть в frontend/app.js;
 *   2) 500 вызовов publishPlayerState() подряд дают ровно 1 POST /api/player/state;
 *   3) за 5 секунд непрерывных вызовов — не больше 6 POST (троттлинг 1/с);
 *   4) отложенный (trailing) POST не теряется — последнее состояние всё-таки уходит.
 */
const fs = require("fs");
const path = require("path");

const APP = path.join(__dirname, "..", "..", "frontend", "app.js");
const START = "// --- троттлинг публикации состояния";
const END = "// --- конец блока троттлинга ---";

let ok = true;
function check(name, cond, extra = "") {
  ok = ok && !!cond;
  console.log(`  [${cond ? "OK " : "FAIL"}] ${name} ${extra}`);
}

const source = fs.readFileSync(APP, "utf8");
const start = source.indexOf(START);
const end = source.indexOf(END);
check("блок троттлинга найден в app.js", start !== -1 && end !== -1);
if (start === -1 || end === -1) {
  console.log("\nИТОГ: ЕСТЬ ОШИБКИ");
  process.exit(1);
}
const block = source.slice(start, end);

// --- песочница: свои часы и свои таймеры ---
let clock = 1000;
const timers = [];
const fakeDate = { now: () => clock };
function fakeSetTimeout(fn, delay) {
  const id = timers.length + 1;
  timers.push({ id, fn, at: clock + (delay || 0) });
  return id;
}
function advance(ms) {
  const target = clock + ms;
  for (;;) {
    const due = timers.filter((t) => t.at <= target).sort((a, b) => a.at - b.at);
    if (!due.length) break;
    const timer = due[0];
    timers.splice(timers.indexOf(timer), 1);
    clock = Math.max(clock, timer.at);
    timer.fn();
  }
  clock = target;
}

const fakeWindow = {};
let posts = 0;
const apiStub = (url) => {
  if (url === "/api/player/state") posts += 1;
  return Promise.resolve({ ok: true });
};
const factory = new Function(
  "Date", "setTimeout", "clearTimeout", "window", "api", "state",
  "currentTrack", "getPosition", "getDuration", "$",
  `${block}
  ;return { publishPlayerState, publishPlayerStateNow };`,
);
const api = factory(
  fakeDate,
  fakeSetTimeout,
  () => {},
  fakeWindow,
  apiStub,
  { playing: true },
  () => ({ id: "1", title: "T", artist: "A", album: "B" }),
  () => 12,
  () => 100,
  () => null,
);

console.log("=== 1. 500 вызовов подряд (одна «секунда») ===");
posts = 0;
for (let i = 0; i < 500; i++) api.publishPlayerState();
check("ровно 1 POST на 500 вызовов", posts === 1, `(получено ${posts})`);
advance(1000);
check("отложенный POST тоже ушёл (итого 2)", posts === 2, `(получено ${posts})`);

console.log("=== 2. непрерывные вызовы 5 секунд ===");
posts = 0;
advance(5000); // сброс окна
for (let t = 0; t < 5000; t += 10) {
  api.publishPlayerState();
  advance(10);
}
advance(1000);
console.log(`  POST за 5 c: ${posts}`);
check("не больше 6 POST за 5 c (троттлинг 1/с)", posts <= 6, `(получено ${posts})`);
check("не меньше 4 POST за 5 c (состояние не теряется)", posts >= 4, `(получено ${posts})`);

console.log("=== 3. одиночный вызов после простоя ===");
posts = 0;
advance(2000);
api.publishPlayerState();
check("после простоя запрос уходит сразу", posts === 1, `(получено ${posts})`);

console.log("\nИТОГ:", ok ? "ВСЁ ОК" : "ЕСТЬ ОШИБКИ");
process.exit(ok ? 0 : 1);
