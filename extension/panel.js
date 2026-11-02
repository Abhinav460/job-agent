const storage = chrome.storage.session;
let connection = null;
let connectionVersion = 0;

const byId = (id) => document.getElementById(id);
const notice = (message) => { byId("notice").textContent = message; };

function parseServerUrl(raw) {
  const url = new URL(raw);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !url.port ||
      url.pathname !== "/" || url.hash || url.username || url.password ||
      url.searchParams.size !== 1 ||
      !/^[A-Za-z0-9_-]{32,128}$/.test(url.searchParams.get("token") || "")) {
    throw new Error("Paste the exact local URL printed by extension-server.");
  }
  return {base: url.origin, token: url.searchParams.get("token")};
}

async function api(path, data) {
  if (!connection) throw new Error("Connect to the local server first.");
  const options = {method: data === undefined ? "GET" : "POST",
                   headers: {"X-JobAgent-Token": connection.token}};
  if (data !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(data);
  }
  const response = await fetch(connection.base + "/api/" + path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "Local action failed.");
  return body;
}

function formValues(form) {
  return Object.fromEntries(new FormData(form).entries());
}

function renderApplications(records) {
  const list = byId("applications");
  list.replaceChildren();
  for (const record of records) {
    const item = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = record.company + " - " + record.title;
    const meta = document.createElement("small");
    meta.textContent = record.status + " | " + record.id;
    item.append(title, meta);
    if (record.status !== "submitted") {
      const open = document.createElement("button");
      open.textContent = "Open form";
      open.type = "button";
      open.addEventListener("click", () => action("review", {id: record.id}));
      item.append(open);
      const submitted = document.createElement("button");
      submitted.textContent = "Record submitted";
      submitted.type = "button";
      submitted.addEventListener("click", () => {
        if (window.confirm("Only record this after you submitted the application yourself. Continue?")) {
          action("submitted", {id: record.id});
        }
      });
      item.append(submitted);
    }
    const sync = document.createElement("button");
    sync.textContent = "Sync sheet";
    sync.type = "button";
    sync.addEventListener("click", () => action("sync", {id: record.id}));
    item.append(sync);
    list.append(item);
  }
}

function loadProfile(profile) {
  const form = byId("profile-form");
  for (const [key, value] of Object.entries(profile || {})) {
    const field = form.elements.namedItem(key);
    if (field) field.value = value;
  }
}

function renderNotes(notes) {
  const list = byId("context-notes");
  list.replaceChildren();
  for (const note of notes) {
    const item = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = note.title;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Delete";
    remove.addEventListener("click", () => {
      if (window.confirm("Delete this saved context note?")) action("context-delete", {id: note.id});
    });
    item.append(title, remove);
    list.append(item);
  }
}

function renderSuggestions(suggestions) {
  const list = byId("context-suggestions");
  list.replaceChildren();
  for (const suggestion of suggestions) {
    const item = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = suggestion.title;
    const body = document.createElement("p");
    body.textContent = suggestion.body;
    item.append(title, body);
    list.append(item);
  }
}

async function refresh({reloadProfile = false} = {}) {
  const state = await api("status");
  byId("summary").textContent = `${state.applications.length} recent applications | ${state.pending} pending sync`;
  byId("sheet-status").textContent = state.sheet_suffix ?
    `Sheet ending ${state.sheet_suffix}` : "No spreadsheet selected";
  byId("folder-list").textContent = state.folders.length ?
    `${state.folders.length} resume folder(s) configured` : "No resume folders configured";
  byId("review-status").textContent = state.review_id ?
    `Review open for ${state.review_id}. Manual actions ${state.manual_actions_enabled ? "enabled" : "paused"}.` :
    "No review browser open.";
  byId("handoff").disabled = !state.review_id || state.manual_actions_enabled;
  byId("close-review").disabled = !state.review_id;
  if (reloadProfile) loadProfile(state.profile);
  renderNotes(state.notes);
  renderApplications(state.applications);
}

async function action(path, data) {
  try {
    const result = await api(path, data);
    notice(result.message || "Done.");
    await refresh({reloadProfile: path === "profile"});
    return result;
  } catch (error) {
    notice(error.message);
  }
}

byId("connect").addEventListener("click", async () => {
  const version = ++connectionVersion;
  try {
    connection = parseServerUrl(byId("server-url").value.trim());
    await refresh({reloadProfile: true});
    if (version !== connectionVersion) return;
    await storage.set({jobAgentConnection: connection});
    byId("server-url").value = "";
    notice("Connected to the local Job Agent server.");
  } catch (error) {
    if (version === connectionVersion) {
      connection = null;
      notice(error.message);
    }
  }
});

byId("use-selection").addEventListener("click", async () => {
  try {
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    if (!tab?.id || !tab.url?.startsWith("https://")) throw new Error("Open an HTTPS job posting first.");
    const [injection] = await chrome.scripting.executeScript({
      target: {tabId: tab.id},
      func: () => ({text: window.getSelection()?.toString() || "", url: location.href, title: document.title})
    });
    const selected = injection.result;
    const description = selected.text.trim();
    if (!description) throw new Error("Select the job description on the page first.");
    if (description.length > 30000) {
      throw new Error("Selected text exceeds 30,000 characters. Select a shorter job description.");
    }
    const form = byId("prepare-form");
    form.elements.namedItem("url").value = selected.url;
    form.elements.namedItem("description").value = description;
    if (!form.elements.namedItem("title").value && selected.title.length <= 160) {
      form.elements.namedItem("title").value = selected.title;
    }
    notice("Selected job text loaded. Check the company and role before preparing.");
  } catch (error) {
    notice(error.message);
  }
});

for (const [formId, endpoint] of [["profile-form", "profile"], ["folder-form", "folder"],
                                  ["prepare-form", "prepare"], ["select-sheet-form", "sheet"],
                                  ["context-add-form", "context-add"]]) {
  byId(formId).addEventListener("submit", async (event) => {
    event.preventDefault();
    await action(endpoint, formValues(event.currentTarget));
  });
}
byId("context-search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const result = await action("context-search", formValues(event.currentTarget));
  if (result) renderSuggestions(result.suggestions || []);
});
byId("sync-pending").addEventListener("click", () => action("sync-pending", {}));
byId("handoff").addEventListener("click", () => action("handoff", {}));
byId("close-review").addEventListener("click", () => action("close", {}));

storage.get("jobAgentConnection").then(async ({jobAgentConnection}) => {
  if (!jobAgentConnection || connectionVersion !== 0) return;
  const version = ++connectionVersion;
  connection = jobAgentConnection;
  try {
    await refresh({reloadProfile: true});
    if (version === connectionVersion) notice("Connected to the local Job Agent server.");
  } catch {
    if (version === connectionVersion) {
      connection = null;
      await storage.remove("jobAgentConnection");
    }
  }
});
