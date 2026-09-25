const $ = (s) => document.querySelector(s);
const STAR_COLORS = { 1: "var(--s1)", 2: "var(--s2)", 3: "var(--s3)", 4: "var(--s4)", 5: "var(--s5)" };
const PAGE_SIZE = 50;

let currentJob = null;
let pollTimer = null;
let reviews = [];
let shown = PAGE_SIZE;

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleDateString("fr-FR", { day: "numeric", month: "short", year: "numeric" });
}

function starsHtml(n) {
  n = Number(n) || 0;
  return `<span class="stars" style="color:${STAR_COLORS[n] || "var(--muted)"}">${"★".repeat(n)}${"☆".repeat(5 - n)}</span>`;
}

// ---------- Lancement ----------
$("#form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = {
    brand: fd.get("brand").trim(),
    stars: fd.getAll("stars").map(Number),
    languages: fd.get("languages"),
    max_pages: fd.get("max_pages") ? Number(fd.get("max_pages")) : null,
    delay: Number(fd.get("delay") || 1.5),
  };
  $("#form-error").hidden = true;
  $("#go").disabled = true;
  try {
    const r = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Requête invalide");
    openJob(data.id);
  } catch (err) {
    $("#form-error").textContent = err.message;
    $("#form-error").hidden = false;
    $("#go").disabled = false;
  }
});

$("#cancel").addEventListener("click", async () => {
  if (!currentJob) return;
  $("#cancel").disabled = true;
  $("#progress-text").textContent = "Arrêt en cours…";
  await fetch(`/api/jobs/${currentJob}/cancel`, { method: "POST" });
});

// ---------- Suivi ----------
function openJob(id) {
  currentJob = id;
  reviews = [];
  history.replaceState(null, "", `#${id}`);
  $("#job").hidden = false;
  $("#results").hidden = true;
  $("#job-error").hidden = true;
  $("#warnings").hidden = true;
  $("#progress-box").hidden = false;
  $("#cancel").disabled = false;
  $("#bar").style.width = "0";
  $("#biz-name").textContent = "Chargement…";
  $("#biz-link").textContent = "";
  $("#biz-score").hidden = true;
  clearTimeout(pollTimer);
  poll();
}

async function poll() {
  const id = currentJob;
  const r = await fetch(`/api/jobs/${id}`);
  if (id !== currentJob) return;
  if (!r.ok) {
    $("#job").hidden = true;
    $("#go").disabled = false;
    return;
  }
  const job = await r.json();
  renderJob(job);
  if (job.status === "running" || job.status === "queued") {
    pollTimer = setTimeout(poll, 1000);
  } else {
    $("#go").disabled = false;
    loadHistory();
    if (job.status === "done" || job.status === "cancelled") loadReviews(id);
  }
}

function renderJob(job) {
  const b = job.business || {};
  $("#biz-name").textContent = b.name || job.domain;
  const url = job.url || `https://www.trustpilot.com/review/${job.domain}`;
  $("#biz-link").textContent = url.replace("https://", "");
  $("#biz-link").href = url;
  if (b.trust_score != null) {
    $("#biz-score").hidden = false;
    $("#biz-trust").textContent = b.trust_score;
    $("#biz-total").textContent = (b.total_reviews ?? "?").toLocaleString("fr-FR");
  }

  const p = job.progress || {};
  const pct = p.pages ? Math.round((p.page / p.pages) * 100) : 0;
  $("#bar").style.width = `${job.status === "done" ? 100 : pct}%`;
  const labels = {
    queued: "En attente…",
    running: p.pages ? `Page ${p.page} / ${p.pages} — ${p.count} avis` : "Connexion à Trustpilot…",
  };
  $("#progress-text").textContent = labels[job.status] || "";
  $("#progress-box").hidden = !(job.status in labels);

  if (job.error) {
    $("#job-error").textContent = job.error;
    $("#job-error").hidden = false;
  }
  const w = [...(job.warnings || [])];
  if (job.status === "cancelled") w.unshift("Extraction arrêtée : résultats partiels.");
  $("#warnings").innerHTML = w.map((x) => `<li>${esc(x)}</li>`).join("");
  $("#warnings").hidden = !w.length;
}

async function loadReviews(id) {
  const r = await fetch(`/api/jobs/${id}?reviews=true`);
  const job = await r.json();
  if (id !== currentJob) return;
  reviews = job.reviews || [];
  const s = job.summary || { count: 0, distribution: {} };
  $("#st-count").textContent = s.count.toLocaleString("fr-FR");
  $("#st-avg").textContent = s.average ?? "–";
  $("#st-replied").textContent = s.replied ?? 0;
  $("#st-verified").textContent = s.verified ?? 0;
  const max = Math.max(1, ...Object.values(s.distribution || {}));
  $("#dist").innerHTML = [5, 4, 3, 2, 1]
    .map((n) => {
      const c = s.distribution?.[n] || 0;
      return `<div class="dist-row"><span>${n}★</span>
        <div class="dist-bar"><div style="width:${(c / max) * 100}%;background:${STAR_COLORS[n]}"></div></div>
        <span class="muted">${c}</span></div>`;
    })
    .join("");
  for (const f of ["csv", "xlsx", "json"]) $(`#ex-${f}`).href = `/api/jobs/${id}/export/${f}`;
  $("#results").hidden = false;
  shown = PAGE_SIZE;
  renderList();
}

// ---------- Liste filtrable ----------
function filtered() {
  const q = $("#q").value.trim().toLowerCase();
  const st = $("#f-stars").value;
  const [key, dir] = $("#f-sort").value.split("-");
  const out = reviews.filter(
    (r) =>
      (!st || String(r.rating) === st) &&
      (!q || `${r.title} ${r.text} ${r.name} ${r.reply}`.toLowerCase().includes(q))
  );
  out.sort((a, b) => {
    const va = key === "rating" ? Number(a.rating) : a.date;
    const vb = key === "rating" ? Number(b.rating) : b.date;
    return (va < vb ? -1 : va > vb ? 1 : 0) * (dir === "asc" ? 1 : -1);
  });
  return out;
}

function highlight(text, q) {
  const safe = esc(text);
  if (!q) return safe;
  const re = new RegExp(esc(q).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
  return safe.replace(re, (m) => `<mark>${m}</mark>`);
}

function renderList() {
  const q = $("#q").value.trim();
  const list = filtered();
  $("#f-count").textContent = `${list.length} avis`;
  $("#list").innerHTML = list
    .slice(0, shown)
    .map(
      (r) => `<article class="review">
      <div class="review-head">
        <span>${starsHtml(r.rating)} <strong>${esc(r.name)}</strong>
          <span class="muted">${esc(r.country)}</span>
          ${r.verified ? '<span class="badge">vérifié</span>' : ""}</span>
        <a class="muted" href="${esc(r.url)}" target="_blank" rel="noopener">${fmtDate(r.date)}</a>
      </div>
      <h4>${highlight(r.title, q)}</h4>
      <p>${highlight(r.text, q)}</p>
      ${r.reply ? `<div class="reply"><strong>Réponse de l'entreprise</strong> <span class="muted">${fmtDate(r.reply_date)}</span><br>${highlight(r.reply, q)}</div>` : ""}
    </article>`
    )
    .join("");
  $("#more").hidden = list.length <= shown;
}

for (const el of ["#q", "#f-stars", "#f-sort"]) {
  $(el).addEventListener("input", () => {
    shown = PAGE_SIZE;
    renderList();
  });
}
$("#more").addEventListener("click", () => {
  shown += PAGE_SIZE;
  renderList();
});

// ---------- Historique ----------
async function loadHistory() {
  const r = await fetch("/api/jobs");
  const jobs = await r.json();
  $("#history-box").hidden = !jobs.length;
  const label = { done: "terminé", cancelled: "arrêté", error: "erreur", running: "en cours", queued: "en attente" };
  $("#history").innerHTML = jobs
    .map(
      (j) => `<li><a href="#${j.id}">${esc(j.business?.name || j.domain)}</a>
      <span class="muted">${j.summary ? j.summary.count + " avis · " : ""}${label[j.status] || j.status}</span></li>`
    )
    .join("");
}

window.addEventListener("hashchange", () => {
  const id = location.hash.slice(1);
  if (id && id !== currentJob) openJob(id);
});

loadHistory();
if (location.hash.length > 1) openJob(location.hash.slice(1));
