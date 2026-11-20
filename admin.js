"use strict";

const $ = (selector) => document.querySelector(selector);
const apiBase = (window.NEWS_EDITOR?.apiUrl || "").replace(/\/$/, "");
const tokenKey = "massage-news-session";
let token = sessionStorage.getItem(tokenKey) || "";
let cards = [];
let revision = 0;
let hasDraft = false;
let dirty = false;
let busy = false;
let editingId = null;
let editingMedia = [];
let formDirty = false;
let mediaBusy = false;
let deleteId = null;
let resumeCardAfterLogin = false;
const previewUrls = new Map();
const dateFormatter = new Intl.DateTimeFormat("ru-RU", { dateStyle: "long" });

function localDate() {
  const now = new Date();
  return [now.getFullYear(), String(now.getMonth() + 1).padStart(2, "0"), String(now.getDate()).padStart(2, "0")].join("-");
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function setStatus(text) {
  $("#editor-status").textContent = text;
}

function updateSaveState() {
  $("#save-state").textContent = dirty ? "Есть несохранённые изменения." : "Черновик сохранён.";
}

function markDirty() {
  dirty = true;
  $("#published-result").hidden = true;
  updateSaveState();
}

function setSession(value) {
  token = value;
  if (value) sessionStorage.setItem(tokenKey, value);
  else sessionStorage.removeItem(tokenKey);
}

async function request(path, { method = "GET", body, blob = false, timeout = 20000 } = {}) {
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(apiBase + path, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal, cache: "no-store", credentials: "omit"
    });
    if (response.ok && blob) return await response.blob();
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 401) {
        if ($("#card-dialog").open) {
          resumeCardAfterLogin = true;
          $("#card-dialog").close();
        }
        setSession("");
        $("#workspace").hidden = true;
        $("#login-panel").hidden = false;
      }
      if (response.status === 409) {
        throw new Error("Черновик изменён в другом окне. Ваши изменения остались здесь. Скопируйте нужные тексты перед обновлением страницы.");
      }
      throw new Error(data.error || "Сервер не смог выполнить действие.");
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("Сервер не ответил вовремя. При публикации проверьте статус или повторите действие: повторная отправка не создаёт копию новости.");
    }
    if (error instanceof TypeError) throw new Error("Не удалось связаться с сервером. Проверьте подключение к интернету.");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

async function run(action) {
  if (busy) return;
  busy = true;
  setStatus("");
  for (const button of document.querySelectorAll("#workspace button, #login-form button")) button.disabled = true;
  try {
    await action();
  } catch (error) {
    setStatus(error.message);
  } finally {
    busy = false;
    for (const button of document.querySelectorAll(".editor-toolbar button, #login-form button")) button.disabled = false;
    $("#publish").disabled = !window.NEWS_EDITOR?.publicationEnabled;
    if (hasDraft) renderCards();
    else if (apiBase) $("#login-panel").hidden = false;
  }
}

function mediaElement(media) {
  const url = previewUrls.get(media.id);
  if (!url) {
    const preview = actionButton(media.kind === "video" ? "Показать видео" : "Показать фото", async () => {
      preview.disabled = true;
      preview.textContent = "Загрузка…";
      try {
        const filename = media.url.split("/").pop();
        if (!/^[a-f0-9]+\.(jpg|jpeg|png|webp|gif|mp4|webm)$/.test(filename)) throw new Error("Неверный адрес файла.");
        const blob = await request("/media/" + filename, { blob: true, timeout: 60000 });
        previewUrls.set(media.id, URL.createObjectURL(blob));
        preview.replaceWith(mediaElement(media));
      } catch (error) {
        preview.textContent = "Повторить загрузку";
        preview.disabled = false;
        setStatus(error.message);
      }
    });
    return preview;
  }
  const node = document.createElement(media.kind === "video" ? "video" : "img");
  node.src = url;
  if (media.kind === "video") {
    node.controls = true;
    node.preload = "metadata";
  } else {
    node.alt = media.name;
    node.loading = "lazy";
  }
  return node;
}

function actionButton(label, callback, disabled = false) {
  const button = element("button", label, "button editor-secondary");
  button.type = "button";
  button.disabled = disabled || busy;
  button.addEventListener("click", callback);
  return button;
}

function moveCard(id, offset) {
  if (busy) return;
  const index = cards.findIndex((card) => card.id === id);
  const target = index + offset;
  if (target < 0 || target >= cards.length) return;
  [cards[index], cards[target]] = [cards[target], cards[index]];
  markDirty();
  renderCards();
  document.getElementById("edit-" + id)?.focus();
}

function renderCards() {
  const container = $("#cards");
  container.replaceChildren();
  $("#empty").hidden = cards.length > 0;
  cards.forEach((card, index) => {
    const article = element("article", undefined, "editor-card");
    const time = element("time", dateFormatter.format(new Date(card.date + "T12:00:00")));
    time.dateTime = card.date;
    article.append(time, element("h2", card.title), element("p", card.text, "card-text"));
    for (const media of card.media) article.append(mediaElement(media));
    const actions = element("div", undefined, "editor-actions");
    const edit = actionButton("Редактировать", () => openCard(card));
    edit.id = "edit-" + card.id;
    actions.append(
      edit,
      actionButton("Выше", () => moveCard(card.id, -1), index === 0),
      actionButton("Ниже", () => moveCard(card.id, 1), index === cards.length - 1),
      actionButton("Удалить", () => {
        if (busy) return;
        deleteId = card.id;
        $("#delete-dialog").showModal();
      })
    );
    article.append(actions);
    container.append(article);
  });
}

function renderMedia() {
  const list = $("#media-list");
  list.replaceChildren();
  editingMedia.forEach((media, index) => {
    const item = element("li");
    item.append(mediaElement(media), element("span", media.name, "media-name"));
    const controls = element("div", undefined, "editor-actions");
    const swap = (offset) => {
      const target = index + offset;
      [editingMedia[index], editingMedia[target]] = [editingMedia[target], editingMedia[index]];
      formDirty = true;
      renderMedia();
    };
    controls.append(
      actionButton("Раньше", () => swap(-1), index === 0 || mediaBusy),
      actionButton("Позже", () => swap(1), index === editingMedia.length - 1 || mediaBusy),
      actionButton("Убрать файл", () => {
        editingMedia.splice(index, 1);
        formDirty = true;
        renderMedia();
      }, mediaBusy)
    );
    item.append(controls);
    list.append(item);
  });
}

function openCard(card) {
  if (busy) return;
  if (!card && cards.length >= 50) {
    setStatus("В ленте можно сохранить до 50 карточек.");
    return;
  }
  editingId = card?.id || null;
  editingMedia = structuredClone(card?.media || []);
  $("#card-dialog-title").textContent = card ? "Редактировать карточку" : "Новая карточка";
  $("#card-title").value = card?.title || "";
  $("#card-date").value = card?.date || localDate();
  $("#card-text").value = card?.text || "";
  $("#card-media").value = "";
  $("#card-status").textContent = "";
  formDirty = false;
  renderMedia();
  $("#card-dialog").showModal();
}

function closeCard() {
  if (mediaBusy || busy) return;
  if (formDirty && !window.confirm("Закрыть карточку без сохранения изменений?")) return;
  formDirty = false;
  $("#card-dialog").close();
}

async function loadDraft() {
  const data = await request("/draft");
  cards = data.cards;
  revision = data.revision;
  hasDraft = true;
  dirty = false;
  renderCards();
  updateSaveState();
}

async function saveDraft() {
  const result = await request("/draft", { method: "PUT", body: { cards, revision } });
  revision = result.revision;
  dirty = false;
  updateSaveState();
}

$("#login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  run(async () => {
    const result = await request("/login", {
      method: "POST", body: { username: $("#username").value, password: $("#password").value }
    });
    setSession(result.token);
    $("#password").value = "";
    if (!hasDraft) await loadDraft();
    if (!token) return;
    $("#login-panel").hidden = true;
    $("#workspace").hidden = false;
    if (resumeCardAfterLogin) {
      resumeCardAfterLogin = false;
      $("#card-dialog").showModal();
    }
    if (dirty) setStatus("Вход выполнен. Несохранённые изменения сохранены в этом окне.");
  });
});

$("#add-card").addEventListener("click", () => openCard());
$("#close-card").addEventListener("click", closeCard);
$("#card-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeCard(); });
$("#card-form").addEventListener("input", () => { formDirty = true; });
$("#card-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (mediaBusy || busy) return;
  const title = $("#card-title").value.trim();
  const text = $("#card-text").value.trim();
  if (!title) {
    $("#card-status").textContent = "Добавьте заголовок карточки.";
    return;
  }
  if (!text && !editingMedia.length) {
    $("#card-status").textContent = "Добавьте текст или медиавложение.";
    return;
  }
  const card = { id: editingId || crypto.randomUUID(), title, text, date: $("#card-date").value, media: structuredClone(editingMedia) };
  if (editingId) cards[cards.findIndex((item) => item.id === editingId)] = card;
  else cards.unshift(card);
  formDirty = false;
  $("#card-dialog").close();
  markDirty();
  renderCards();
});

function fileData(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(",")[1]);
    reader.onerror = () => reject(new Error("Не удалось прочитать файл."));
    reader.readAsDataURL(file);
  });
}

$("#card-media").addEventListener("change", async (event) => {
  const files = [...event.target.files];
  if (editingMedia.length + files.length > 5) {
    $("#card-status").textContent = "В одной карточке может быть до 5 файлов.";
    event.target.value = "";
    return;
  }
  if (files.some((file) => !file.size || file.size > 20 * 1024 * 1024)) {
    $("#card-status").textContent = "Каждый файл должен быть размером до 20 МБ и содержать данные.";
    event.target.value = "";
    return;
  }
  mediaBusy = true;
  $("#card-form button[type=submit]").disabled = true;
  $("#close-card").disabled = true;
  $("#card-media").disabled = true;
  renderMedia();
  try {
    for (const file of files) {
      $("#card-status").textContent = `Загружается файл «${file.name}»…`;
      const media = await request("/upload", {
        method: "POST", body: { name: file.name, data: await fileData(file) }, timeout: 120000
      });
      previewUrls.set(media.id, URL.createObjectURL(file));
      editingMedia.push(media);
      formDirty = true;
    }
    $("#card-status").textContent = "Файлы загружены. Нажмите «Готово», чтобы добавить их в карточку.";
  } catch (error) {
    $("#card-status").textContent = error.message;
  } finally {
    mediaBusy = false;
    $("#card-form button[type=submit]").disabled = false;
    $("#close-card").disabled = false;
    $("#card-media").disabled = false;
    event.target.value = "";
    renderMedia();
  }
});

$("#cancel-delete").addEventListener("click", () => $("#delete-dialog").close());
$("#confirm-delete").addEventListener("click", () => {
  if (busy) return;
  cards = cards.filter((card) => card.id !== deleteId);
  $("#delete-dialog").close();
  markDirty();
  renderCards();
});
$("#save-draft").addEventListener("click", () => run(async () => {
  await saveDraft();
  setStatus("Черновик сохранён. Публичная лента обновится после публикации.");
}));
$("#publish").addEventListener("click", () => run(async () => {
  if (!window.NEWS_EDITOR?.publicationEnabled) return;
  if (dirty) await saveDraft();
  setStatus("Отправляем изменения в GitHub…");
  const result = await request("/publish", { method: "POST", body: { revision }, timeout: 180000 });
  const output = $("#published-result");
  output.replaceChildren(
    element("h2", "Отправлено в GitHub"),
    element("p", "GitHub Pages обновит ленту после завершения сборки. Это может занять несколько минут.")
  );
  const link = element("a", "Открыть ленту", "text-link");
  link.href = "./news.html";
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  output.append(link, element("p", `Коммит ${result.commit?.slice(0, 7) || "создан"}`));
  output.hidden = false;
  setStatus("Публикация отправлена.");
}));
$("#logout").addEventListener("click", () => run(async () => {
  if (dirty && !window.confirm("Выйти без сохранения изменений?")) return;
  await request("/logout", { method: "POST", body: {} });
  setSession("");
  cards = [];
  hasDraft = false;
  dirty = false;
  for (const url of previewUrls.values()) URL.revokeObjectURL(url);
  previewUrls.clear();
  $("#cards").replaceChildren();
  $("#workspace").hidden = true;
  $("#login-panel").hidden = false;
}));
window.addEventListener("beforeunload", (event) => {
  if (dirty || formDirty || busy || mediaBusy) { event.preventDefault(); event.returnValue = ""; }
});

$("#publication-setup").hidden = Boolean(window.NEWS_EDITOR?.publicationEnabled);
if (!apiBase) {
  $("#setup").hidden = false;
} else if (!/^https:\/\//.test(apiBase) && !/^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(apiBase)) {
  $("#setup").hidden = false;
  setStatus("Адрес сервера должен использовать HTTPS.");
} else if (token) {
  run(async () => {
    await loadDraft();
    if (token) $("#workspace").hidden = false;
  });
} else {
  $("#login-panel").hidden = false;
}
