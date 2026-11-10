"use strict";

/** doctor: report the resolved environment, Python and token without starting anything. */

const { existsSync } = require("node:fs");

const { resolveEnvironment } = require("../env");
const { cliVenvPython, findPython, readVersion } = require("../runtime");
const { readToken } = require("../token");

/**
 * Returns {
 *   version, environment, host, port, dataRoot,
 *   python: command line string or null,
 *   tokenFound,
 *   cliEnvReady: boolean (cli environment) | null (other environments)
 * }.
 */
async function runDoctor(options) {
  await resolveEnvironment(options);
  const python = options.environment === "cli"
    ? { command: cliVenvPython(), args: [] }
    : options.python
      ? { command: options.python, args: [] }
      : findPython(options.data);
  return {
    version: readVersion(),
    environment: options.environment,
    host: options.host,
    port: options.port,
    dataRoot: options.data,
    python: python && python.command ? [python.command, ...python.args].join(" ") : null,
    tokenFound: Boolean(readToken(options)),
    cliEnvReady: options.environment === "cli" ? existsSync(cliVenvPython()) : null,
  };
}

module.exports = { runDoctor };
