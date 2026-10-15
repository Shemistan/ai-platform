// Вкладка «Аудио»: текст → mp3 через narrator. Очередь, прогресс, плеер, история.
import { $, api, h, logBlock, progressBar, seconds, statusChip } from "/ui.js";

const API = "/api/audio";
const state = { config: null, jobs: [], selected: null };

function engine() {
  return state.config.engines.find((e) => e.id === $("engine").value);
}

function fillVoices() {
  const e = engine();
  $("voice").replaceChildren(...e.voices.map((v) => h("option", { value: v, selected: v === e.default_voice }, v)));
  $("speed-field").hidden = !e.speed;
  $("engine-note").textContent = e.note;
}

function renderJob(job) {
  const box = $("job");
  if (!job) {
    box.replaceChildren(h("p", { class: "muted" }, "Здесь появится озвучка."));
    return;
  }
  const active = job.status === "queued" || job.status === "running";
  box.replaceChildren(
    h(
      "div",
      { class: "job" },
      h(
        "div",
        { class: "job-head" },
        statusChip(job.status),
        h("span", { class: "muted small" }, `${job.engine_label} · ${job.voice}${job.speed ? ` · ×${job.speed}` : ""} · ${job.chars} знаков`),
      ),
      h("p", { class: "job-text" }, job.preview),
      active ? progressBar(job.progress) : null,
      job.queue_position ? h("p", { class: "muted small" }, `Перед ней в очереди: ${job.queue_position}`) : null,
      job.error ? h("p", { class: "job-error" }, job.error) : null,
      job.audio_url ? h("audio", { controls: true, src: job.audio_url, preload: "metadata" }) : null,
      h(
        "div",
        { class: "actions" },
        job.audio_url ? h("a", { class: "link", href: `${job.audio_url}?download=1` }, "Скачать mp3") : null,
        job.duration_s ? h("span", { class: "muted small" }, `Длительность ${seconds(job.duration_s)}`) : null,
        job.elapsed_s ? h("span", { class: "muted small" }, `Озвучка заняла ${seconds(job.elapsed_s)}`) : null,
        active ? h("button", { type: "button", class: "link danger", onclick: () => cancel(job.id) }, "Отменить") : null,
      ),
      logBlock(job.log),
    ),
  );
}

function renderHistory() {
  $("history").replaceChildren(
    ...(state.jobs.length
      ? state.jobs.map((job) =>
          h(
            "li",
            {},
            h(
              "button",
              { type: "button", class: `history-item${state.selected?.id === job.id ? " current" : ""}`, onclick: () => select(job.id) },
              h("span", { class: "history-text" }, h("span", { class: "history-title" }, job.preview), h("span", { class: "muted small" }, `${job.engine_label} · ${job.voice}`)),
              statusChip(job.status),
            ),
          ),
        )
      : [h("li", { class: "muted" }, "Пока пусто.")]),
  );
}

async function select(id) {
  state.selected = await api("GET", `${API}/jobs/${id}`);
  renderJob(state.selected);
  renderHistory();
}

async function cancel(id) {
  state.selected = await api("POST", `${API}/jobs/${id}/cancel`);
  renderJob(state.selected);
}

async function poll() {
  try {
    const { jobs } = await api("GET", `${API}/jobs`);
    state.jobs = jobs;
    if (state.selected) {
      const fresh = jobs.find((j) => j.id === state.selected.id);
      if (fresh && (fresh.status !== state.selected.status || fresh.status === "running" || fresh.status === "queued")) {
        state.selected = await api("GET", `${API}/jobs/${fresh.id}`);
        renderJob(state.selected);
      }
    }
    renderHistory();
  } catch {
    /* сервер мог перезапуститься — следующий опрос попробует снова */
  }
  const active = state.jobs.some((j) => j.status === "queued" || j.status === "running");
  setTimeout(poll, active ? 1000 : 5000);
}

async function submit(event) {
  event.preventDefault();
  const error = $("form-error");
  error.hidden = true;
  const payload = { text: $("text").value, engine: $("engine").value, voice: $("voice").value };
  if (engine().speed) payload.speed = Number($("speed").value);
  $("submit").disabled = true;
  try {
    state.selected = await api("POST", `${API}/jobs`, payload);
    renderJob(state.selected);
    state.jobs = [state.selected, ...state.jobs];
    renderHistory();
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  } finally {
    $("submit").disabled = false;
  }
}

async function init() {
  state.config = await api("GET", `${API}/config`);
  $("engine").replaceChildren(
    ...state.config.engines.map((e) => h("option", { value: e.id, selected: e.id === state.config.default_engine }, e.label)),
  );
  $("engine").addEventListener("change", fillVoices);
  fillVoices();
  const [low, high] = state.config.speed_range;
  Object.assign($("speed"), { min: low, max: high });
  $("speed").addEventListener("input", () => ($("speed-out").textContent = $("speed").value));
  $("text").addEventListener("input", () => ($("text-count").textContent = `${$("text").value.length} знаков из ${state.config.max_text}`));
  $("composer").addEventListener("submit", submit);
  poll();
}

init().catch((err) => {
  $("job").replaceChildren(h("p", { class: "job-error" }, `Не удалось загрузить настройки: ${err.message}`));
});
