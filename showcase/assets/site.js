// Shared by every page: shows which deploy you are looking at.
// version.json is written at deploy time; locally it does not exist and nothing is shown.
"use strict";

(async () => {
  const slot = document.getElementById("build");
  if (!slot) return;
  try {
    const resp = await fetch(`${document.body.dataset.root || ""}version.json`, { cache: "no-store" });
    if (!resp.ok) return;
    const v = await resp.json();
    slot.textContent = ` · build ${v.commit} · ${v.date}`;
  } catch {
    // offline or no stamp: leave the footer as is
  }
})();
