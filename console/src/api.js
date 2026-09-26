/** Frappe method call using session cookie + CSRF from www context. */
export async function call(method, args = {}) {
  const csrf =
    window.csrf_token ||
    document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") ||
    "";
  const res = await fetch(`/api/method/${method}`, {
    method: "POST",
    credentials: "include",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "X-Frappe-CSRF-Token": csrf,
    },
    body: JSON.stringify(args),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.exc_type || data.exception) {
    const msg =
      data._server_messages ||
      data.message ||
      data.exc_type ||
      `Request failed (${res.status})`;
    let parsed = msg;
    try {
      const arr = JSON.parse(data._server_messages || "[]");
      if (Array.isArray(arr) && arr.length) {
        parsed = arr
          .map((m) => {
            try {
              return JSON.parse(m).message;
            } catch {
              return m;
            }
          })
          .join("; ");
      }
    } catch {
      /* keep */
    }
    throw new Error(typeof parsed === "string" ? parsed : JSON.stringify(parsed));
  }
  return data.message ?? data;
}
