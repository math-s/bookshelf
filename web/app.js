/* Bookshelf UI. Vanilla JS, no build step.
 *
 * One `state` object drives both views, so switching between grid and table
 * never loses the filters you set. */

import { Config, api } from "./config.js";

const STATUSES = ["want", "reading", "read", "abandoned"];
const STATUS_LABEL = { want: "Want to read", reading: "Reading", read: "Read", abandoned: "Abandoned" };

const state = {
  q: "",
  status: new Set(),
  tags: new Set(),
  authorId: null,
  ratingMin: null,
  sort: "title",
  order: "asc",
  view: localStorage.getItem("bookshelf.view") || "grid",
  multiAuthorsOnly: localStorage.getItem("bookshelf.multiOnly") === "1",
  items: [],
  groups: null,
  total: 0,
  loadError: null,
  facets: { statuses: {}, tags: [], authors: [] },
};

const $ = (sel) => document.querySelector(sel);
/** Is this a plain options bag, as opposed to a child node or text? */
const isProps = (value) =>
  value != null && typeof value === "object" && !value.nodeType && !Array.isArray(value);

/** el("div", {class...}, ...children) — the props argument is optional, so a
 *  child may legitimately appear in second position. */
const el = (tag, ...rest) => {
  const props = isProps(rest[0]) ? rest.shift() : {};
  const node = Object.assign(document.createElement(tag), props);
  for (const child of rest.flat()) {
    if (child != null && child !== false) {
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
  }
  return node;
};

/* --- feedback ------------------------------------------------------------- */

let toastTimer;
function toast(message, isError = false) {
  document.querySelector(".toast")?.remove();
  const node = el("div", { className: `toast${isError ? " error" : ""}`, textContent: message });
  document.body.append(node);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.remove(), isError ? 5000 : 2200);
}

/* --- loading -------------------------------------------------------------- */

function queryString() {
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  for (const s of state.status) params.append("status", s);
  for (const t of state.tags) params.append("tag", t);
  if (state.authorId) params.set("author_id", state.authorId);
  if (state.ratingMin) params.set("rating_min", state.ratingMin);
  params.set("sort", state.sort);
  params.set("order", state.order);
  params.set("limit", "500");
  if (state.view === "author") {
    params.set("group_by", "author");
    if (state.multiAuthorsOnly) params.set("min_books", "2");
  }
  return params.toString();
}

async function load() {
  try {
    const data = await api(`/api/books?${queryString()}`);
    // The grouped endpoint returns `groups`; the flat one returns `items`.
    state.groups = data.groups || null;
    state.items = data.items || (data.groups || []).flatMap((g) => g.books);
    state.total = data.total;
    state.facets = data.facets;
    state.loadError = null;
    render();
  } catch (err) {
    state.loadError = err.message;
    state.items = [];
    state.total = 0;
    render();
    toast(err.message, true);
  }
}

/* --- rendering ------------------------------------------------------------ */

function render() {
  renderFacets();
  renderResults();
  $("#count").textContent = state.facets.statuses
    ? `· ${Object.values(state.facets.statuses).reduce((a, b) => a + b, 0)} books`
    : "";
  const filtered = state.q || state.status.size || state.tags.size || state.authorId || state.ratingMin;
  $("#reset-wrap").hidden = !filtered;
  $("#multi-only").hidden = state.view !== "author";
  $("#summary").textContent = filtered
    ? `${state.total} matching book${state.total === 1 ? "" : "s"}`
    : `${state.total} book${state.total === 1 ? "" : "s"}`;
}

function renderFacets() {
  const statusBox = $("#facet-status");
  statusBox.replaceChildren(
    ...STATUSES.map((s) => {
      const checkbox = el("input", { type: "checkbox", checked: state.status.has(s) });
      checkbox.addEventListener("change", () => {
        checkbox.checked ? state.status.add(s) : state.status.delete(s);
        load();
      });
      return el("label", { className: "facet" }, checkbox, STATUS_LABEL[s],
        el("span", { className: "count", textContent: state.facets.statuses?.[s] ?? 0 }));
    })
  );

  const ratingBox = $("#facet-rating");
  ratingBox.replaceChildren(
    ...[4, 3, 2].map((n) => {
      const active = state.ratingMin === n;
      const button = el("button", {
        className: "chip",
        textContent: `${"★".repeat(n)}${"☆".repeat(5 - n)} & up`,
      });
      button.setAttribute("aria-pressed", String(active));
      button.addEventListener("click", () => {
        state.ratingMin = active ? null : n;
        load();
      });
      return el("div", { style: "padding:2px 0" }, button);
    })
  );

  const tagBox = $("#facet-tags");
  if (!state.facets.tags?.length) {
    tagBox.replaceChildren(el("span", { className: "count", textContent: "No tags yet" }));
  } else {
    tagBox.replaceChildren(
      ...state.facets.tags.map(({ name, count }) => {
        const active = state.tags.has(name);
        const chip = el("button", { className: "chip", textContent: `${name} ${count}` });
        chip.setAttribute("aria-pressed", String(active));
        chip.addEventListener("click", () => {
          active ? state.tags.delete(name) : state.tags.add(name);
          load();
        });
        return chip;
      })
    );
  }

  const authorBox = $("#facet-authors");
  const authors = (state.facets.authors || []).slice(0, 12);
  authorBox.replaceChildren(
    ...authors.map(({ id, name, count }) => {
      const active = state.authorId === id;
      const row = el("div", { className: "facet" },
        el("span", { textContent: name, style: active ? "color:var(--accent);font-weight:600" : "" }),
        el("span", { className: "count", textContent: count }));
      row.addEventListener("click", () => {
        state.authorId = active ? null : id;
        load();
      });
      return row;
    })
  );
}

function coverNode(book) {
  const wrap = el("div", { className: "cover" });
  if (book.thumbnail_url) {
    const img = el("img", { src: book.thumbnail_url, alt: "", loading: "lazy" });
    // A dead cover URL should degrade to the placeholder, not a broken icon.
    img.addEventListener("error", () => img.replaceWith(placeholderNode(book)));
    wrap.append(img);
  } else {
    wrap.append(placeholderNode(book));
  }
  wrap.append(el("div", { className: `pip ${book.status}`, title: STATUS_LABEL[book.status] }));
  return wrap;
}

function placeholderNode(book) {
  return el("div", { className: "placeholder" },
    el("b", { textContent: book.title || "Untitled" }),
    el("span", { textContent: (book.authors || []).join(", ") }));
}

function starsNode(rating) {
  const node = el("span", { className: "stars" });
  for (let i = 1; i <= 5; i++) {
    node.append(el("span", { className: i <= (rating || 0) ? "" : "off", textContent: "★" }));
  }
  return node;
}

function renderResults() {
  const box = $("#results");
  if (state.loadError) {
    const settings = el("button", { className: "primary", textContent: "Open settings" });
    settings.addEventListener("click", openSettings);
    box.replaceChildren(el("div", { className: "empty" },
      el("h3", { textContent: "Can't reach the API" }),
      el("p", { textContent: state.loadError }),
      el("p", {}, settings)));
    return;
  }
  if (!state.items.length) {
    box.replaceChildren(el("div", { className: "empty" },
      el("h3", { textContent: state.total === 0 && !state.q ? "No books yet" : "Nothing matched" }),
      el("p", { textContent: state.total === 0 && !state.q
        ? "Add some ISBNs to get started." : "Try loosening the filters." })));
    return;
  }
  if (state.view === "author") box.replaceChildren(authorGroupsNode());
  else box.replaceChildren(state.view === "grid" ? gridNode() : tableNode());
}

/** Books bucketed under each author. A book with two authors shows under both. */
function authorGroupsNode() {
  const wrap = el("div", { className: "groups" });
  for (const group of state.groups || []) {
    const heading = el("h3", { className: "grouphead" },
      el("button", { className: "linkish groupname", textContent: group.name }),
      el("span", { className: "count", textContent: `${group.books.length} book${group.books.length === 1 ? "" : "s"}` }));

    if (group.id != null) {
      heading.querySelector(".groupname").addEventListener("click", () => {
        state.authorId = group.id;
        setView("grid");
        load();
      });
    }

    wrap.append(heading, el("div", { className: "grid" },
      ...group.books.map((book) => {
        const card = el("div", { className: "card" },
          coverNode(book),
          el("h4", { textContent: book.title || "Untitled" }),
          el("p", { textContent: book.status }));
        card.addEventListener("click", () => openDrawer(book.isbn13));
        return card;
      })));
  }
  if (!(state.groups || []).length) {
    wrap.append(el("div", { className: "empty" }, el("h3", { textContent: "Nothing matched" })));
  }
  return wrap;
}

function gridNode() {
  return el("div", { className: "grid" },
    ...state.items.map((book) => {
      const card = el("div", { className: "card" },
        coverNode(book),
        el("h4", { textContent: book.title || "Untitled" }),
        el("p", { textContent: (book.authors || []).join(", ") || "Unknown author" }));
      card.addEventListener("click", () => openDrawer(book.isbn13));
      return card;
    }));
}

function tableNode() {
  const columns = [
    ["title", "Title"], ["author", "Author"], [null, "Status"], ["rating", "Rating"],
    [null, "Tags"], ["published", "Published"], ["pages", "Pages"],
  ];
  const head = el("tr", ...columns.map(([key, label]) => {
    const th = el("th", { textContent: label + (state.sort === key ? (state.order === "asc" ? " ▲" : " ▼") : "") });
    if (key) {
      th.addEventListener("click", () => {
        state.order = state.sort === key && state.order === "asc" ? "desc" : "asc";
        state.sort = key;
        $("#sort").value = key;
        $("#order").value = state.order;
        load();
      });
    } else {
      th.style.cursor = "default";
    }
    return th;
  }));

  const body = el("tbody", ...state.items.map((book) => {
    const select = el("select");
    for (const s of STATUSES) {
      select.append(el("option", { value: s, textContent: STATUS_LABEL[s], selected: book.status === s }));
    }
    select.addEventListener("click", (e) => e.stopPropagation());
    select.addEventListener("change", async () => {
      await patchBook(book.isbn13, { status: select.value }, `Marked "${book.title}" ${STATUS_LABEL[select.value].toLowerCase()}`);
    });

    const row = el("tr",
      el("td", { className: "title" }, book.title || "Untitled"),
      el("td", (book.authors || []).join(", ")),
      el("td", select),
      el("td", starsNode(book.rating)),
      el("td", (book.tags || []).join(", ")),
      el("td", book.published_date || ""),
      el("td", book.page_count == null ? "" : String(book.page_count)));
    row.addEventListener("click", () => openDrawer(book.isbn13));
    return row;
  }));

  return el("div", { className: "tablewrap" }, el("table", el("thead", head), body));
}

/* --- drawer --------------------------------------------------------------- */

function closeOverlays() {
  document.querySelectorAll(".scrim, .drawer, .modal").forEach((n) => n.remove());
}

async function patchBook(isbn13, patch, successMessage) {
  const book = state.items.find((b) => b.isbn13 === isbn13);
  const previous = book ? { ...book } : null;
  if (book) Object.assign(book, patch);   // optimistic
  renderResults();
  try {
    const updated = await api(`/api/books/${isbn13}`, { method: "PATCH", body: JSON.stringify(patch) });
    if (book) Object.assign(book, updated);
    if (successMessage) toast(successMessage);
    renderFacets();
    renderResults();
    return updated;
  } catch (err) {
    if (book && previous) Object.assign(book, previous);   // roll back
    renderResults();
    toast(`Could not save: ${err.message}`, true);
    throw err;
  }
}

async function openDrawer(isbn13) {
  let book;
  try {
    book = await api(`/api/books/${isbn13}`);
  } catch (err) {
    return toast(`Could not open book: ${err.message}`, true);
  }
  closeOverlays();

  const scrim = el("div", { className: "scrim" });
  scrim.addEventListener("click", closeOverlays);

  const statusSelect = el("select");
  for (const s of STATUSES) {
    statusSelect.append(el("option", { value: s, textContent: STATUS_LABEL[s], selected: book.status === s }));
  }
  statusSelect.addEventListener("change", () => save({ status: statusSelect.value }));

  const starPick = el("div", { className: "starpick" });
  const paintStars = (value) => {
    starPick.replaceChildren(
      ...[1, 2, 3, 4, 5].map((n) => {
        const b = el("button", { textContent: "★", className: n <= (value || 0) ? "on" : "", title: `${n} star${n > 1 ? "s" : ""}` });
        b.addEventListener("click", () => { paintStars(n); save({ rating: n }); });
        return b;
      }),
      (() => {
        const clear = el("button", { className: "clear", textContent: "clear" });
        clear.addEventListener("click", () => { paintStars(null); save({ rating: null }); });
        return clear;
      })()
    );
  };
  paintStars(book.rating);

  const tagsInput = el("input", { type: "text", value: (book.tags || []).join(", "),
    placeholder: "sci-fi, owned, lent to Ana" });
  tagsInput.addEventListener("change", async () => {
    const tags = tagsInput.value.split(",").map((t) => t.trim()).filter(Boolean);
    try {
      const result = await api(`/api/books/${isbn13}/tags`, { method: "PUT", body: JSON.stringify({ tags }) });
      tagsInput.value = result.tags.join(", ");
      toast("Tags saved");
      load();
    } catch (err) {
      toast(`Could not save tags: ${err.message}`, true);
    }
  });

  const notes = el("textarea", { value: book.notes || "", placeholder: "What did you make of it?" });
  notes.addEventListener("change", () => save({ notes: notes.value }));

  const started = el("input", { type: "date", value: book.started_on || "" });
  started.addEventListener("change", () => save({ started_on: started.value || null }));
  const finished = el("input", { type: "date", value: book.finished_on || "" });
  finished.addEventListener("change", () => save({ finished_on: finished.value || null }));

  async function save(patch) {
    try {
      Object.assign(book, await patchBook(isbn13, patch));
    } catch { /* patchBook already reported and rolled back */ }
  }

  const remove = el("button", { className: "danger", textContent: "Remove from library" });
  remove.addEventListener("click", async () => {
    if (!confirm(`Remove "${book.title}" from your library?`)) return;
    try {
      await api(`/api/books/${isbn13}`, { method: "DELETE" });
      closeOverlays();
      toast("Removed");
      load();
    } catch (err) {
      toast(`Could not remove: ${err.message}`, true);
    }
  });

  const close = el("button", { className: "close", textContent: "✕", title: "Close" });
  close.addEventListener("click", closeOverlays);

  const drawer = el("div", { className: "drawer" },
    close,
    el("div", { className: "hero" },
      coverNode(book),
      el("div", {},
        el("h2", { textContent: book.title || "Untitled" }),
        book.subtitle ? el("p", { className: "byline", textContent: book.subtitle }) : null,
        authorLinksNode(book))),
    el("dl", {},
      ...detailPairs(book).flatMap(([k, v]) => [el("dt", { textContent: k }), el("dd", { textContent: v })])),
    book.description ? el("div", { className: "desc", textContent: book.description }) : null,
    el("fieldset", {}, el("legend", { textContent: "Your notes" }),
      el("div", { className: "field" }, el("label", { textContent: "Status" }), statusSelect),
      el("div", { className: "field" }, el("label", { textContent: "Rating" }), starPick),
      el("div", { className: "field" }, el("label", { textContent: "Tags (comma separated)" }), tagsInput),
      el("div", { className: "field" }, el("label", { textContent: "Started" }), started),
      el("div", { className: "field" }, el("label", { textContent: "Finished" }), finished),
      el("div", { className: "field" }, el("label", { textContent: "Notes" }), notes)),
    el("div", { style: "margin-top:16px" }, remove));

  document.body.append(scrim, drawer);
}

/** Author names that filter the library down to that person when clicked. */
function authorLinksNode(book) {
  const credits = book.author_credits || [];
  if (!credits.length) {
    return el("p", { className: "byline", textContent: "Unknown author" });
  }
  const line = el("p", { className: "byline" });
  credits.forEach((credit, index) => {
    if (index) line.append(", ");
    const link = el("button", { className: "linkish", textContent: credit.name });
    link.addEventListener("click", () => {
      state.authorId = credit.id;
      closeOverlays();
      load();
    });
    line.append(link);
  });
  return line;
}

function detailPairs(book) {
  const pairs = [];
  if (book.publisher) pairs.push(["Publisher", book.publisher]);
  if (book.published_date) pairs.push(["Published", book.published_date]);
  if (book.page_count) pairs.push(["Pages", String(book.page_count)]);
  if (book.language) pairs.push(["Language", book.language]);
  if (book.categories?.length) pairs.push(["Categories", book.categories.join(", ")]);
  pairs.push(["ISBN", book.isbn13]);
  pairs.push(["Source", book.source]);
  return pairs;
}

/* --- import modal --------------------------------------------------------- */

function openImport() {
  closeOverlays();
  const scrim = el("div", { className: "scrim" });
  scrim.addEventListener("click", closeOverlays);

  const textarea = el("textarea", { placeholder: "9780140328721\n0-261-10221-4\n978-0-7475-3269-9" });
  const report = el("div", { className: "report" });
  const submit = el("button", { className: "primary", textContent: "Import" });
  const cancel = el("button", { textContent: "Cancel" });
  cancel.addEventListener("click", closeOverlays);

  submit.addEventListener("click", async () => {
    const text = textarea.value.trim();
    if (!text) return toast("Paste some ISBNs first", true);
    submit.disabled = true;
    submit.textContent = "Importing…";
    report.replaceChildren();
    try {
      const result = await api("/api/import", { method: "POST", body: JSON.stringify({ text }) });
      report.replaceChildren(
        el("div", { textContent:
          `${result.added.length} added, ${result.updated.length} updated, ` +
          `${result.skipped.length} already present, ${result.failed_count} failed.` }),
        result.failures.length
          ? el("ul", ...result.failures.map((f) => el("li", { textContent: `${f.input} — ${f.reason}` })))
          : null);
      if (result.added.length) textarea.value = "";
      load();
    } catch (err) {
      report.replaceChildren(el("div", { style: "color:var(--abandoned)", textContent: err.message }));
    } finally {
      submit.disabled = false;
      submit.textContent = "Import";
    }
  });

  document.body.append(scrim, el("div", { className: "modal" },
    el("h2", { textContent: "Add ISBNs" }),
    el("p", { className: "hint", textContent:
      "One per line. ISBN-10 and ISBN-13 both work, hyphens are fine, and lines starting with # are ignored." }),
    textarea, report,
    el("div", { className: "actions" }, cancel, submit)));
}

/* --- settings modal ------------------------------------------------------- */

function openSettings() {
  closeOverlays();
  const scrim = el("div", { className: "scrim" });
  scrim.addEventListener("click", closeOverlays);

  const base = el("input", { type: "text", value: Config.apiBase,
    placeholder: "https://your-app.fly.dev" });
  const tokenInput = el("input", { type: "password", value: Config.token,
    placeholder: "value of BOOKSHELF_TOKEN" });

  const save = el("button", { className: "primary", textContent: "Save" });
  save.addEventListener("click", () => {
    Config.apiBase = base.value;
    Config.token = tokenInput.value;
    closeOverlays();
    toast("Settings saved");
    load();
  });
  const cancel = el("button", { textContent: "Cancel" });
  cancel.addEventListener("click", closeOverlays);

  document.body.append(scrim, el("div", { className: "modal" },
    el("h2", { textContent: "Settings" }),
    el("p", { className: "hint", textContent:
      "Where this page should send its API calls. Leave the server URL blank when the " +
      "API serves this page itself. Both values are stored in this browser only." }),
    el("div", { className: "field" }, el("label", { textContent: "API server URL" }), base),
    el("div", { className: "field" }, el("label", { textContent: "Access token" }), tokenInput),
    el("div", { className: "actions" }, cancel, save)));
}

/* --- wiring --------------------------------------------------------------- */

function setView(view) {
  const previous = state.view;
  state.view = view;
  localStorage.setItem("bookshelf.view", view);
  for (const name of ["grid", "table", "author"]) {
    $(`#view-${name}`).setAttribute("aria-pressed", String(view === name));
  }
  // Grouped and flat results come from different shapes, so switching in or out
  // of the author view needs a refetch rather than just a re-render.
  if ((previous === "author") !== (view === "author")) load();
  else renderResults();
}

let searchTimer;
$("#q").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = e.target.value.trim(); load(); }, 220);
});
$("#view-grid").addEventListener("click", () => setView("grid"));
$("#view-table").addEventListener("click", () => setView("table"));
$("#view-author").addEventListener("click", () => setView("author"));
$("#min-books").addEventListener("change", (e) => {
  state.multiAuthorsOnly = e.target.checked;
  localStorage.setItem("bookshelf.multiOnly", e.target.checked ? "1" : "0");
  load();
});
$("#min-books").checked = state.multiAuthorsOnly;
$("#sort").addEventListener("change", (e) => { state.sort = e.target.value; load(); });
$("#order").addEventListener("change", (e) => { state.order = e.target.value; load(); });
$("#open-import").addEventListener("click", openImport);
$("#open-settings").addEventListener("click", openSettings);
$("#reset").addEventListener("click", () => {
  state.q = ""; state.status.clear(); state.tags.clear();
  state.authorId = null; state.ratingMin = null;
  $("#q").value = "";
  load();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeOverlays();
  if (e.key === "/" && document.activeElement !== $("#q")) { e.preventDefault(); $("#q").focus(); }
});

// setView only refetches when the grouped/flat shape changes, which it never
// does on the first call — so the initial load is unconditional.
setView(state.view);
load();
