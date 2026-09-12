// Zero-dependency stand-in for `vite --port 5173 --strictPort --host 127.0.0.1`.
// Ignores whatever CLI args RuntimeManager's fixed command table appends (it
// always passes the same four flags) and just binds to the same fixed port,
// so the RuntimeManager/Docker integration test can exercise a real
// create->install->start->health-check->stop->destroy cycle without needing
// a real Vite/React dependency tree.
const http = require("http");

const server = http.createServer((req, res) => {
  res.writeHead(200, { "Content-Type": "text/plain" });
  res.end("ok");
});

server.listen(5173, "127.0.0.1", () => {
  console.log("fixture dev server listening on 127.0.0.1:5173");
});
