import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { api } from "./api";
import { I18nProvider } from "./i18n";
import { pairFromFragment, watchPairingLinks } from "./lib/phonePairing";
import "./styles.css";
import "./i18n/styles.css";

const pairingPage = { location: window.location, history: window.history, fetch: window.fetch.bind(window), base: api.base };
// A QR link scanned while the app is already open: pair, then load everything again.
watchPairingLinks(window, pairingPage, () => window.location.reload());
// A phone opening the QR link pairs before the app makes its first request.
void pairFromFragment(pairingPage)
  .finally(() => {
    createRoot(document.getElementById("root")!).render(
      <React.StrictMode>
        <I18nProvider><App /></I18nProvider>
      </React.StrictMode>,
    );
  });
