chrome.sidePanel.setPanelBehavior({openPanelOnActionClick: true});

// Relay for the on-page Autofill button. Pages never see the server URL or token:
// the content script asks here, and only this extension origin calls the local server.
async function callServer(path, data) {
  const {jobAgentConnection: connection} = await chrome.storage.session.get("jobAgentConnection");
  if (!connection) throw new Error("Open the Job Agent side panel and connect to the local server first.");
  const response = await fetch(connection.base + "/api/" + path, {
    method: "POST",
    headers: {"X-JobAgent-Token": connection.token, "Content-Type": "application/json"},
    body: JSON.stringify(data),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "Local action failed.");
  return body;
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id || !sender.tab) return false;
  let request;
  if (message?.type === "autofill" && typeof message.fields === "string") {
    request = callServer("autofill", {fields: message.fields});
  } else if (message?.type === "applied" && typeof message.url === "string") {
    request = callServer("applied", {url: message.url, company: String(message.company || ""),
                                     title: String(message.title || "")});
  } else {
    return false;
  }
  request.then(reply, (error) => reply({error: error.message}));
  return true; // async reply
});
