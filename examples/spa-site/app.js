// Minimal client-side router (History API). Deep links like /about only work when the host
// falls back to index.html for unknown paths — that is what the SPA option tests.
const routes = {
  "/": ["홈", "Self-Service Cloud Platform으로 배포한 SPA 테스트 페이지입니다."],
  "/about": ["소개", "이 페이지는 클라이언트 라우터가 그립니다. 새로고침해도 열리면 SPA fallback이 동작하는 것입니다."],
  "/docs/getting-started": ["문서", "깊은 경로(/docs/getting-started)도 index.html로 연결됩니다."],
};

function render() {
  const [title, text] = routes[location.pathname] || ["404", `없는 페이지예요: ${location.pathname}`];
  const view = document.getElementById("view");
  view.replaceChildren();
  const h1 = document.createElement("h1");
  h1.textContent = title;
  const p = document.createElement("p");
  p.textContent = text;
  const path = document.createElement("p");
  path.className = "path";
  path.textContent = `현재 경로: ${location.pathname}`;
  view.append(h1, p, path);
  document.title = `${title} · SSCP SPA test`;
}

document.addEventListener("click", (e) => {
  const a = e.target.closest("a[data-link]");
  if (!a) return;
  e.preventDefault();
  history.pushState(null, "", a.getAttribute("href"));
  render();
});
window.addEventListener("popstate", render);
render();
