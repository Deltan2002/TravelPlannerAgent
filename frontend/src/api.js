function errorMessage(payload, status) {
  if (typeof payload?.detail === "string") return payload.detail;
  if (Array.isArray(payload?.detail)) {
    return payload.detail.map((item) => item.msg || "Invalid value").join("; ");
  }
  return "Request failed with status " + status;
}

async function request(path, options = {}) {
  const response = await fetch("/api" + path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...options.headers,
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(errorMessage(payload, response.status));
  return payload;
}

export function createPlan(body) {
  return request("/plan", { method: "POST", body: JSON.stringify(body) });
}

export function getPlan(planId) {
  return request("/plan/" + encodeURIComponent(planId));
}

export function reviewPlan(planId, body) {
  return request("/plan/" + encodeURIComponent(planId) + "/review", {
    method: "POST",
    body: JSON.stringify(body),
  });
}
