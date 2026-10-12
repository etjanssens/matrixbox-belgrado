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
  sz: { prefix: "sz/", tz: "Europe/Ljubljana", kind: "dated" },
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
const REALTIME_CACHE_SECONDS = 30; // Renfe ververst de live feeds elke 30 seconden
const LOOKBACK_MINUTES = 120; // vertraagde treinen die volgens de dienstregeling al weg zijn

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const now = new Date();
    const feed = FEEDS[url.searchParams.get("operator")] || FEEDS.bg;
    try {
      if (url.pathname === "/get_departures") {
        const ids = (url.searchParams.get("station") || "").split("-").filter((id) => /^\d+$/.test(id));
        return reply(await departures(env, feed, ids, now), feed, now);
      }
      if (url.pathname === "/search_stop") {
        return reply(await searchStops(env, feed, url.searchParams.get("station") || ""), feed, now);
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
  const res = await fetch(env.DATA_URL.replace(/\/$/, "") + "/" + feed.prefix + path, {
    cf: { cacheTtl: DATA_CACHE_SECONDS, cacheEverything: true },
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error("data " + res.status);
  return res.json();
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
