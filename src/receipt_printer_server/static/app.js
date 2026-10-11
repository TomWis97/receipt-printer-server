// The web UI: pick a template, fill in its fields, print or preview.
// Every field builder returns {element, read}, where read() gives the value
// to send. The server does all validation; this only collects input.

const templateSelect = document.getElementById("template");
const description = document.getElementById("description");
const fieldsBox = document.getElementById("fields");
const form = document.getElementById("form");
const printButton = document.getElementById("print");
const previewButton = document.getElementById("preview");
const result = document.getElementById("result");
const markup = document.getElementById("markup");
const statusButton = document.getElementById("status");

let templates = [];
let readForm = () => ({});
let nextId = 0;

function make(tag, properties, children) {
  const element = Object.assign(document.createElement(tag), properties || {});
  for (const child of children || []) element.append(child);
  return element;
}

function labelled(field, input) {
  input.id = "field-" + nextId++;
  const parts = [make("label", { htmlFor: input.id, textContent: field.label }), input];
  if (field.help) parts.push(make("p", { className: "help", textContent: field.help }));
  return make("div", {}, parts);
}

function buildField(field) {
  const value = field.default;

  if (field.type === "boolean") {
    const input = make("input", { type: "checkbox", checked: Boolean(value) });
    const label = make("label", { className: "checkbox" }, [input, field.label]);
    return { element: label, read: () => input.checked };
  }

  if (field.type === "choice") {
    const input = make("select", {}, field.options.map((option) =>
      make("option", { value: option, textContent: option })));
    if (!field.required) input.prepend(make("option", { value: "", textContent: "" }));
    input.value = value == null ? "" : value;
    return { element: labelled(field, input), read: () => input.value };
  }

  if (field.type === "list") return buildList(field);
  if (field.type === "image") return buildImage(field);

  let input;
  if (field.type === "string" && field.multiline) {
    input = make("textarea");
  } else if (field.type === "integer" || field.type === "number") {
    input = make("input", { type: "number", step: field.type === "integer" ? "1" : "any" });
    if (field.min != null) input.min = field.min;
    if (field.max != null) input.max = field.max;
  } else {
    input = make("input", { type: "text" });
    if (field.max_length) input.maxLength = field.max_length;
  }
  input.value = value == null ? "" : value;
  input.required = field.required;
  return { element: labelled(field, input), read: () => input.value };
}

function buildList(field) {
  const items = make("div");
  const rows = [];

  function addItem() {
    const subfields = field.fields.map((subfield) => [subfield.name, buildField(subfield)]);
    const remove = make("button", { type: "button", className: "small", textContent: "Remove" });
    const fieldset = make("fieldset", {}, subfields.map(([, built]) => built.element).concat([remove]));
    const row = { fieldset, subfields };
    remove.onclick = () => {
      rows.splice(rows.indexOf(row), 1);
      fieldset.remove();
    };
    rows.push(row);
    items.append(fieldset);
  }

  const add = make("button", { type: "button", className: "small", textContent: "+ Add" });
  add.onclick = addItem;
  for (let i = 0; i < (field.min_items || 1); i++) addItem();

  const element = make("div", {}, [make("label", { textContent: field.label }), items, add]);
  const read = () => rows.map((row) =>
    Object.fromEntries(row.subfields.map(([name, built]) => [name, built.read()])));
  return { element, read };
}

function buildImage(field) {
  const input = make("input", { type: "file", accept: "image/*" });
  let dataUrl = "";
  input.onchange = () => {
    dataUrl = "";
    const file = input.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => { dataUrl = reader.result; };
    reader.readAsDataURL(file);
  };
  return { element: labelled(field, input), read: () => dataUrl };
}

function showTemplate(name) {
  const template = templates.find((t) => t.name === name);
  fieldsBox.replaceChildren();
  result.hidden = true;
  markup.hidden = true;
  description.textContent = template.description || "";

  if (template.error) {
    showResult("This template is broken:\n" + template.error, false);
    printButton.disabled = previewButton.disabled = true;
    readForm = () => ({});
    return;
  }
  printButton.disabled = previewButton.disabled = false;
  if (!template.fields.length) {
    fieldsBox.append(make("p", { className: "help", textContent: "This template takes no input." }));
  }
  const built = template.fields.map((field) => [field.name, buildField(field)]);
  for (const [, field] of built) fieldsBox.append(field.element);
  readForm = () => Object.fromEntries(built.map(([name, field]) => [name, field.read()]));
  try { localStorage.setItem("template", name); } catch (error) { /* only a convenience */ }
}

function showResult(message, good) {
  result.textContent = message;
  result.className = good ? "" : "bad";
  result.hidden = false;
}

async function send(action) {
  const name = templateSelect.value;
  printButton.disabled = previewButton.disabled = true;
  markup.hidden = true;
  try {
    const response = await fetch(`/api/templates/${encodeURIComponent(name)}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(readForm()),
    });
    const body = await response.json();
    if (!response.ok) {
      let message = body.error;
      for (const [path, problem] of Object.entries(body.fields || {})) {
        message += `\n• ${path}: ${problem}`;
      }
      showResult(message, false);
    } else if (action === "preview") {
      showResult(`Renders to ${body.bytes} bytes of printer commands.`, true);
      markup.textContent = body.markup;
      markup.hidden = false;
    } else {
      showResult("Printed.", true);
    }
  } catch (error) {
    showResult("Could not reach the server: " + error, false);
  } finally {
    printButton.disabled = previewButton.disabled = false;
    refreshStatus();
  }
}

async function refreshStatus() {
  try {
    const status = await (await fetch("/api/status")).json();
    statusButton.textContent = status.status;
    statusButton.className = "status " + (status.ok ? "good" : "bad");
  } catch (error) {
    statusButton.textContent = "server unreachable";
    statusButton.className = "status bad";
  }
}

async function start() {
  templates = await (await fetch("/api/templates")).json();
  for (const template of templates) {
    const label = template.title + (template.error ? " (broken)" : "");
    templateSelect.append(make("option", { value: template.name, textContent: label }));
  }
  let remembered = null;
  try { remembered = localStorage.getItem("template"); } catch (error) { /* only a convenience */ }
  if (templates.some((t) => t.name === remembered)) templateSelect.value = remembered;
  if (templates.length) showTemplate(templateSelect.value);
  refreshStatus();
}

templateSelect.onchange = () => showTemplate(templateSelect.value);
form.onsubmit = (event) => { event.preventDefault(); send("print"); };
previewButton.onclick = () => send("preview");
statusButton.onclick = refreshStatus;
start();
