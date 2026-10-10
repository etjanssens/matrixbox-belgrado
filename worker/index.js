// Cloudflare Worker die de MatrixBox (T-Skylt-firmware) vertrektijden voor Belgrado geeft.
// Hij spreekt dezelfde taal als data.t-skylt.se:
//   /get_departures?country=rs&operator=bg&station=20067      (meerdere haltes: 20067-20068)
//   /search_stop?country=rs&operator=bg&station=slavija
//   /get_deviations?...                                         (altijd leeg: geen live data)
// De tijden komen uit de dienstregeling (GTFS) die de GitHub Action naar GitHub Pages publiceert.

const TZ = "Europe/Belgrade";
const MAX_DEPARTURES = 15; // de MatrixBox ontvangt in kleine stukjes; houd het antwoord compact
const MAX_SEARCH_RESULTS = 30;
const CACHE_SECONDS = 6 * 3600;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const now = new Date();
    try {
      if (url.pathname === "/get_departures") {
        const ids = (url.searchParams.get("station") || "").split("-").filter((id) => /^\d+$/.test(id));
        return reply(await departures(env, ids, now), now);
      }
      if (url.pathname === "/search_stop") {
        return reply(await searchStops(env, url.searchParams.get("station") || ""), now);
      }
      if (url.pathname === "/get_deviations") {
        return reply([], now);
      }
      return reply({ status: "ok", info: "MatrixBox Belgrado" }, now);
    } catch (e) {
      return reply({ departures: [], msg: "Error: " + e.message }, now);
    }
  },
};

// ---------------------------------------------------------------- data ophalen

async function getJson(env, path) {
  const res = await fetch(env.DATA_URL.replace(/\/$/, "") + "/" + path, {
    cf: { cacheTtl: CACHE_SECONDS, cacheEverything: true },
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error("data " + res.status);
  return res.json();
}

// ---------------------------------------------------------------- tijd in Belgrado

// Lokale datum/tijd in Belgrado, plus de afwijking t.o.v. UTC in minuten.
function belgradeNow(now) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-GB", {
      timeZone: TZ, hourCycle: "h23",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    }).formatToParts(now).map((p) => [p.type, p.value])
  );
  const localAsUtc = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second);
  return {
    y: +parts.year, m: +parts.month, d: +parts.day,
    minutes: +parts.hour * 60 + +parts.minute,
    seconds: +parts.second,
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

async function departures(env, ids, now) {
  if (!ids.length) return { departures: [], msg: "No station" };
  const [calendar, ...stops] = await Promise.all([
    getJson(env, "calendar.json"),
    ...ids.slice(0, 3).map((id) => getJson(env, "dep/" + id + ".json")),
  ]);

  const local = belgradeNow(now);
  const nowMin = local.minutes;
  const found = [];

  // Gisteren (ritten na middernacht, GTFS-tijden >= 24:00), vandaag en morgen (voor laat op de avond).
  for (const delta of [-1, 0, 1]) {
    const d = day(local, delta);
    const shift = delta * 1440; // minuten t.o.v. vandaag middernacht
    for (const service of servicesOn(calendar, d)) {
      for (const stop of stops) {
        if (!stop || !stop[service]) continue;
        for (const [min, li] of stop[service]) {
          const t = min + shift;
          if (t < nowMin) continue;
          if (t > nowMin + 24 * 60) break;
          found.push([t, stop.l[li]]);
        }
      }
    }
  }

  found.sort((a, b) => a[0] - b[0]);
  const today = day(local, 0).base;
  return {
    departures: found.slice(0, MAX_DEPARTURES).map(([t, [line, mode, headsign, dir]]) => ({
      destination: headsign,
      direction_code: dir,
      // Lokale tijd in Belgrado, zonder tijdzone, net als data.t-skylt.se
      expected: new Date(today + t * 60000).toISOString().slice(0, 19),
      line: { id: line, transport_mode: mode },
      deviations: [],
    })),
  };
}

// ---------------------------------------------------------------- haltes zoeken

// Kleine letters, zonder accenten, zodat "slavija", "Čukarica" en "cukarica" allemaal werken.
function plain(s) {
  return s.toLowerCase().replace(/đ/g, "dj").normalize("NFD").replace(/[̀-ͯ]/g, "");
}

async function searchStops(env, query) {
  const q = plain(query.trim());
  if (q.length < 2) return {};
  const stops = await getJson(env, "stops.json");
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
function localDateHeader(now) {
  const l = belgradeNow(now);
  const dt = new Date(l.localAsUtc);
  const pad = (n) => String(n).padStart(2, "0");
  const sign = l.offset >= 0 ? "+" : "-";
  const off = Math.abs(l.offset);
  return DAYS[dt.getUTCDay()] + ", " + pad(dt.getUTCDate()) + " " + MONTHS[dt.getUTCMonth()] + " " + dt.getUTCFullYear() +
    " " + pad(dt.getUTCHours()) + ":" + pad(dt.getUTCMinutes()) + ":" + pad(dt.getUTCSeconds()) +
    " " + sign + pad(Math.floor(off / 60)) + pad(off % 60);
}

function reply(body, now) {
  return new Response(JSON.stringify(body), {
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "X-Local-Time": localDateHeader(now),
      "Cache-Control": "no-store",
    },
  });
}
