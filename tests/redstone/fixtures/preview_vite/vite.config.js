// A Windows host's bind mount does not deliver inotify events into the
// Linux container, so file watching must poll to notice edits made on the
// host (by Redstone's agent). Host and port come from Redstone's command.
export default {
  server: { watch: { usePolling: true, interval: 200 } },
};
