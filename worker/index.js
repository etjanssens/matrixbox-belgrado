// Cloudflare Worker die de MatrixBox (T-Skylt-firmware) vertrektijden geeft voor vervoerders die
// data.t-skylt.se niet kent. Hij spreekt dezelfde taal als data.t-skylt.se:
//   /get_departures?country=rs&operator=bg&station=20067        (meerdere haltes: 20067-20068)
//   /search_stop?country=es&operator=renfe&station=atocha
//   /get_deviations?...                                           (altijd leeg)
// Welke data hij gebruikt hangt af van `operator` (zie FEEDS). De dienstregelingen komen van GitHub Pages
// (gebouwd door de GitHub Action); bij Renfe komen daar live vertragingen (GTFS-RT) overheen.

const FEEDS = {
  // Belgrado: vaste weekdienstregeling (calendar.json), geen live data
  bg: { prefix: "", tz: "Europe/Belgrade", kind: "weekly" },
  // ÖBB en HŽPP: per datum uitgerekende ritten (geen open live data)
  oebb: { prefix: "oebb/", tz: "Europe/Vienna", kind: "dated" },
  hzpp: { prefix: "hzpp/", tz: "Europe/Zagreb", kind: "dated" },
  cp: { prefix: "cp/", tz: "Europe/Lisbon", kind: "dated" },
  euskotren: { prefix: "euskotren/", tz: "Europe/Madrid", kind: "dated" },
  // Estland: dienstregeling; voor het stadsvervoer van Tallinn live tijden (transport.tallinn.ee)
  ee: { prefix: "ee/", tz: "Europe/Tallinn", kind: "dated", live: "tallinn" },
  gr: { prefix: "gr/", tz: "Europe/Athens", kind: "dated" },
  cz: { prefix: "cz/", tz: "Europe/Prague", kind: "dated" },
  vivi: { prefix: "vivi/", tz: "Europe/Riga", kind: "dated" },
  rs: { prefix: "rs/", tz: "Europe/Riga", kind: "dated" },
  atd: { prefix: "atd/", tz: "Europe/Riga", kind: "dated" },
  // Stadsbussen Ljubljana: live van de open LPP-API, met de dienstregeling als reserve
  lpp: { prefix: "lpp/", tz: "Europe/Ljubljana", kind: "dated", live: "lpp" },
  // Slovenië (NAP, alleen met account): staat niet op de openbare Pages maar privé in R2, in bundels
  sz: { prefix: "sz/", tz: "Europe/Ljubljana", kind: "dated", r2: true, shards: 128 },
  // Oostenrijk compleet (Mobilitätsverbünde, account + eigen licentie): ook privé in R2
  atrail: { prefix: "atrail/", tz: "Europe/Vienna", kind: "dated", r2: true, shards: 128 },
  // Trenitalia + Italo: weekdienstregeling (geen open live data)
  treni: { prefix: "italia/", tz: "Europe/Rome", kind: "weekly" },
  // Renfe: per datum uitgerekende ritten + live vertragingen en uitvallers
  renfe: {
    prefix: "renfe/", tz: "Europe/Madrid", kind: "dated",
    realtime: ["https://gtfsrt.renfe.com/trip_updates.json", "https://gtfsrt.renfe.com/trip_updates_LD.json"],
  },
};
const MAX_DEPARTURES = 15; // de MatrixBox ontvangt in kleine stukjes; houd het antwoord compact
const MAX_SEARCH_RESULTS = 30;
const DATA_CACHE_SECONDS = 3600;
const DATA_CACHE_VERSION = 2; // ophogen om oude kopieën in de Cloudflare-cache te negeren
const REALTIME_CACHE_SECONDS = 30; // Renfe ververst de live feeds elke 30 seconden
const LOOKBACK_MINUTES = 120; // vertraagde treinen die volgens de dienstregeling al weg zijn

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const now = new Date();
    const feed = FEEDS[url.searchParams.get("operator")] || FEEDS.bg;
    // Doorgeefluik voor één open bron die GitHub weigert (ATD Letland, CC0). Vaste URL: geen open proxy.
    if (url.pathname === "/mirror/atd-gtfs.zip") {
      return fetch("https://www.atd.lv/sites/default/files/GTFS/gtfs-latvia-lv.zip", { cf: { cacheTtl: 3600 } });
    }
    try {
      if (url.pathname === "/get_departures") {
        const ids = (url.searchParams.get("station") || "").split("-").filter((id) => /^\d+$/.test(id));
        return reply(await departures(env, feed, ids, now), feed, now);
      }
      if (url.pathname === "/search_stop") {
        // De MatrixBox toont elke sleutel als station: bij een fout dus een lege lijst, geen foutobject
        try {
          return reply(await searchStops(env, feed, url.searchParams.get("station") || ""), feed, now);
        } catch (e) {
          return reply({}, feed, now);
        }
      }
      if (url.pathname === "/get_deviations") {
        return reply([], feed, now);
      }
      return reply({ status: "ok", feeds: Object.keys(FEEDS) }, feed, now);
    } catch (e) {
      return reply({ departures: [], msg: "Error: " + e.message }, feed, now);
    }
  },
};

// ---------------------------------------------------------------- data ophalen

async function getJson(env, feed, path) {
  if (feed.r2) return getPrivateJson(env, feed, path);
  // ?v=: nieuwe cache-sleutels na een wijziging; "niet gevonden" maar kort bewaren (een land kan net online komen)
  const res = await fetch(env.DATA_URL.replace(/\/$/, "") + "/" + feed.prefix + path + "?v=" + DATA_CACHE_VERSION, {
    cf: { cacheTtlByStatus: { "200-299": DATA_CACHE_SECONDS, "404": 60, "500-599": 0 }, cacheEverything: true },
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error("data " + res.status);
  return res.json();
}

// Privé-data uit R2. dep/<id>.json zit in bundel shards/<id % shards>.json (zie scripts/shard.py).
async function getPrivateJson(env, feed, path) {
  const dep = path.match(/^dep\/(\d+)\.json$/);
  const key = feed.prefix + (dep ? "shards/" + (Number(dep[1]) % feed.shards) + ".json" : path);
  const obj = await env.PRIVATE.get(key);
  if (!obj) return null;
  const data = await obj.json();
  return dep ? data[dep[1]] || null : data;
}

// trip_id -> {delay (seconden), canceled}. Een kapotte live feed mag de dienstregeling niet blokkeren.
async function realtime(feed) {
  const trips = new Map();
  await Promise.all((feed.realtime || []).map(async (url) => {
    try {
      const res = await fetch(url, { cf: { cacheTtl: REALTIME_CACHE_SECONDS, cacheEverything: true } });
      if (!res.ok) return;
      for (const e of (await res.json()).entity || []) {
        const tu = e.tripUpdate;
        if (!tu) continue;
        const stu = (tu.stopTimeUpdate || [])[0];
        const delay = tu.delay ?? stu?.departure?.delay ?? stu?.arrival?.delay ?? 0;
        trips.set(tu.trip.tripId, { delay: +delay || 0, canceled: tu.trip.scheduleRelationship === "CANCELED" });
      }
    } catch (e) {
      console.log("realtime", url, e.message);
    }
  }));
  return trips;
}

// ---------------------------------------------------------------- lokale tijd

// Lokale datum/tijd in de tijdzone van de feed, plus de afwijking t.o.v. UTC in minuten.
function localNow(tz, now) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-GB", {
      timeZone: tz, hourCycle: "h23",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    }).formatToParts(now).map((p) => [p.type, p.value])
  );
  const localAsUtc = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second);
  return {
    y: +parts.year, m: +parts.month, d: +parts.day,
    minutes: +parts.hour * 60 + +parts.minute,
    offset: Math.round((localAsUtc - now.getTime()) / 60000),
    localAsUtc,
  };
}

// Kalenderdag `delta` dagen na de lokale datum, als {key: "YYYYMMDD", weekday: 0=maandag, base: ms om middernacht}
function day(local, delta) {
  const base = Date.UTC(local.y, local.m - 1, local.d + delta);
  const dt = new Date(base);
  const key = dt.toISOString().slice(0, 10).replace(/-/g, "");
  return { key, weekday: (dt.getUTCDay() + 6) % 7, base };
}

function servicesOn(calendar, d) {
  const active = new Set();
  for (const [id, c] of Object.entries(calendar.weekly)) {
    if (c.days.includes(d.weekday) && c.start <= d.key && d.key <= c.end) active.add(id);
  }
  for (const id of calendar.removed[d.key] || []) active.delete(id);
  for (const id of calendar.added[d.key] || []) active.add(id);
  return active;
}

// ---------------------------------------------------------------- vertrektijden

// Alle geplande vertrekken van gisteren t/m morgen als {t: minuten t.o.v. vandaag middernacht, info, trip}.
// Gisteren is nodig voor ritten na middernacht (GTFS-tijden >= 24:00), morgen voor laat op de avond.
function* scheduled(feed, calendar, stops, local) {
  for (const delta of [-1, 0, 1]) {
    const d = day(local, delta);
    const shift = delta * 1440;
    for (const stop of stops) {
      if (!stop) continue;
      if (feed.kind === "dated") {
        for (const [min, li, trip] of stop.d[d.key] || []) yield { t: min + shift, info: stop.l[li], trip };
      } else {
        for (const service of servicesOn(calendar, d)) {
          for (const [min, li] of stop[service] || []) yield { t: min + shift, info: stop.l[li] };
        }
      }
    }
  }
}

async function departures(env, feed, ids, now) {
  if (!ids.length) return { departures: [], msg: "No station" };
  if (feed.live === "lpp") {
    const live = await lppLive(env, feed, ids, now);
    if (live) return live;
  }
  const [calendar, live, ...stops] = await Promise.all([
    feed.kind === "weekly" ? getJson(env, feed, "calendar.json") : null,
    realtime(feed),
    ...ids.slice(0, 3).map((id) => getJson(env, feed, "dep/" + id + ".json")),
  ]);

  const local = localNow(feed.tz, now);
  const nowSec = local.minutes * 60;
  const found = [];
  const seen = new Set(); // sommige bronnen bevatten dezelfde rit dubbel
  for (const dep of scheduled(feed, calendar, stops, local)) {
    if (dep.t < local.minutes - LOOKBACK_MINUTES || dep.t > local.minutes + 1440) continue;
    const rt = dep.trip && live.get(dep.trip);
    const sec = dep.t * 60 + (rt && !rt.canceled ? rt.delay : 0);
    // Uitgevallen treinen blijven zichtbaar tot hun geplande vertrektijd
    if (sec < nowSec) continue;
    const key = sec + "|" + dep.info[0] + "|" + dep.info[2];
    if (seen.has(key)) continue;
    seen.add(key);
    found.push({ sec, info: dep.info, rt });
  }

  if (feed.live === "tallinn") {
    // Live tijden van het Tallinnse stadsvervoer vervangen de geplande ritten met vlag (info[5]); de rest blijft
    const live = await tallinnLive(env, feed, ids, local);
    if (live) {
      for (let i = found.length - 1; i >= 0; i--) if (found[i].info[5]) found.splice(i, 1);
      found.push(...live);
    }
  }
  found.sort((a, b) => a.sec - b.sec);
  const today = day(local, 0).base;
  return {
    departures: found.slice(0, MAX_DEPARTURES).map(({ sec, info: [line, mode, headsign, dir, color], rt }) => ({
      destination: rt?.canceled ? "CANC " + headsign : headsign,
      direction_code: dir || 0,
      // Lokale tijd, zonder tijdzone, net als data.t-skylt.se
      expected: new Date(today + sec * 1000).toISOString().slice(0, 19),
      line: { id: line, transport_mode: mode },
      // Lijnkleur (hex, zonder #); de aangepaste MatrixBox kleurt daarmee de lijnaanduiding bij Renfe
      color: color || "",
      // "#" = live data bekend; de MatrixBox toont dat teken als "rt_indicator" aanstaat
      deviations: rt ? "#" : [],
    })),
  };
}

// LPP (Ljubljana): live aankomsten per halte van data.lpp.si. type 0 = live gevolgd, 2 = komt nu aan,
// 1 = volgens dienstregeling. Geeft null als de API niets bruikbaars teruggeeft (dan de dienstregeling).
function niceName(s) {
  if (s !== s.toUpperCase()) return s;
  // "N. STOŽICE P+R" -> "N. Stožice P+R": woorden met een hoofdletter, afkortingen van 1-2 letters (SŽ, P+R) blijven
  return s.replace(/\p{L}+/gu, (w) => (w.length <= 2 ? w : w[0] + w.slice(1).toLowerCase()));
}

async function lppLive(env, feed, ids, now) {
  try {
    const [colors, ...answers] = await Promise.all([
      getJson(env, feed, "lines.json").catch(() => null),
      ...ids.slice(0, 3).map((id) =>
        fetch("https://data.lpp.si/api/station/arrival?station-code=" + id, { cf: { cacheTtl: 15, cacheEverything: true } })
          .then((r) => (r.ok ? r.json() : null)).catch(() => null)),
    ]);
    const arrivals = answers.flatMap((a) => (a && a.data && a.data.arrivals) || []);
    if (!answers.some((a) => a && a.data)) return null;
    const local = localNow(feed.tz, now);
    const seen = new Set();
    const out = [];
    for (const a of arrivals.sort((x, y) => x.eta_min - y.eta_min)) {
      const dest = niceName((a.stations && a.stations.arrival) || a.trip_name || "");
      const key = a.route_name + "|" + dest + "|" + a.eta_min;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({
        destination: dest,
        direction_code: 0,
        expected: new Date(local.localAsUtc + a.eta_min * 60000).toISOString().slice(0, 19),
        line: { id: a.route_name, transport_mode: "BUS" },
        color: (colors && colors[a.route_name]) || "",
        deviations: a.type === 1 ? [] : "#",
      });
      if (out.length >= MAX_DEPARTURES) break;
    }
    return { departures: out };
  } catch (e) {
    return null;
  }
}

// Tallinn: transport.tallinn.ee/siri-stop-departures.php?stopid=<GTFS stop_id> (open, CSV):
//   Transport,RouteNum,ExpectedTimeInSeconds,ScheduleTimeInSeconds,<nu>,version…
//   stop,1001
//   bus,1,48688,48684,Viimsi,376,Z          (tijden in seconden na middernacht)
async function tallinnLive(env, feed, ids, local) {
  try {
    const [colors, ...answers] = await Promise.all([
      getJson(env, feed, "lines.json").catch(() => null),
      ...ids.slice(0, 3).map((id) =>
        fetch("https://transport.tallinn.ee/siri-stop-departures.php?stopid=" + id, { cf: { cacheTtl: 15, cacheEverything: true } })
          .then((r) => (r.ok ? r.text() : null)).catch(() => null)),
    ]);
    if (!answers.some((t) => t && t.startsWith("Transport"))) return null;
    const out = [];
    const nowSec = local.minutes * 60;
    for (const text of answers) {
      for (const row of (text || "").split("\n").slice(2)) {
        const f = row.trim().split(",");
        if (f.length < 5 || !/^\d+$/.test(f[2])) continue;
        let sec = Number(f[2]);
        if (sec < nowSec - 3600) sec += 86400; // na middernacht
        const tram = f[0] === "tram";
        const line = tram ? "T" + f[1] : f[1];
        out.push({ sec, info: [line, tram ? "TRAM" : "BUS", niceName(f[4]), 0, (colors && colors[line]) || (tram ? "016e12" : "")], rt: { delay: 0 } });
      }
    }
    return out;
  } catch (e) {
    return null;
  }
}

// ---------------------------------------------------------------- haltes zoeken

// Kleine letters, zonder accenten, zodat "slavija", "Čukarica"/"cukarica" en "Málaga"/"malaga" allemaal werken.
function plain(s) {
  return s.toLowerCase().replace(/đ/g, "dj").normalize("NFD").replace(/[̀-ͯ]/g, "");
}

async function searchStops(env, feed, query) {
  // "Hbf" -> "Hauptbahnhof" (ÖBB schrijft het voluit)
  const q = plain(query.trim()).replace(/\bhbf\b/g, "hauptbahnhof");
  if (q.length < 2) return {};
  const stops = await getJson(env, feed, "stops.json");
  if (!stops) return {};
  const hits = stops.filter(([id, code, name]) => plain(name).includes(q) || code === q);

  const out = {};
  const byName = {};
  for (const [id, code, name, hint] of hits.slice(0, MAX_SEARCH_RESULTS)) {
    const label = name + " [" + code + "]" + (hint ? " > " + hint : "");
    out[label.slice(0, 70)] = id;
    (byName[name] = byName[name] || []).push(id);
  }
  // Extra keuze per naam: alle perrons/richtingen van die halte samen (max 3, zoals de MatrixBox aankan).
  for (const [name, ids] of Object.entries(byName)) {
    if (ids.length > 1) out[name + " (all, " + Math.min(ids.length, 3) + " stops)"] = ids.slice(0, 3).join("-");
  }
  return out;
}

// ---------------------------------------------------------------- antwoord

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// De MatrixBox haalt zijn klok uit de Date-header. Cloudflare maakt daar altijd GMT van (en dan telt de firmware
// er vast 2 uur bij op, fout in de wintertijd). Daarom sturen we lokale tijd mee in X-Local-Time, die de
// aangepaste firmware voorrang geeft, in hetzelfde formaat als data.t-skylt.se ("... 14:10:27 +0200").
function localDateHeader(tz, now) {
  const l = localNow(tz, now);
  const dt = new Date(l.localAsUtc);
  const pad = (n) => String(n).padStart(2, "0");
  const sign = l.offset >= 0 ? "+" : "-";
  const off = Math.abs(l.offset);
  return DAYS[dt.getUTCDay()] + ", " + pad(dt.getUTCDate()) + " " + MONTHS[dt.getUTCMonth()] + " " + dt.getUTCFullYear() +
    " " + pad(dt.getUTCHours()) + ":" + pad(dt.getUTCMinutes()) + ":" + pad(dt.getUTCSeconds()) +
    " " + sign + pad(Math.floor(off / 60)) + pad(off % 60);
}

function reply(body, feed, now) {
  return new Response(JSON.stringify(body), {
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "X-Local-Time": localDateHeader(feed.tz, now),
      "Cache-Control": "no-store",
    },
  });
}
