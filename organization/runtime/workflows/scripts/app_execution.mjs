/** The authorized desktop host injects the documented MCP callbacks directly.
 * No HTTP/IPC discovery, dynamic method names, worker response injection or
 * creation retry. A test double proves behavior only, never live App acceptance.
 */
export function createAppHostAdapter({ hostTools, journal }) {
  const names = ['list_projects', 'create_thread', 'list_threads', 'read_thread',
    'send_message_to_thread', 'wait_threads'];
  if (!journal || !['next', 'accept', 'lost'].every(k => typeof journal[k] === 'function') ||
      !hostTools || !names.every(k => typeof hostTools[k] === 'function')) {
    throw new TypeError('explicit_host_capabilities_required');
  }
  // Capture exact callbacks now; later property replacement cannot switch tools.
  const capabilities = Object.freeze(Object.fromEntries(names.map(k => [k, hostTools[k]])));
  return Object.freeze({
    async step(operationId) {
      const next = await journal.next(operationId);
      if (next.status !== 'invoke') return next;
      if (!Object.hasOwn(capabilities, next.method)) throw new TypeError('unsupported_host_tool');
      let result;
      try {
        result = await capabilities[next.method](next.args);
      } catch {
        return await journal.lost(next.token);
      }
      // No API accepts a caller-supplied result as a successful step.
      return await journal.accept(next.token, result);
    }
  });
}
