/**
 * Entry point.
 *
 * StrictMode is deliberately left off: its double-invoked effects would create
 * two WebGL contexts and two sockets on every mount in development, which makes
 * the render loop and the reconnect logic much harder to read that it needs to be.
 */

import { createRoot } from 'react-dom/client';

import App from './App';
import './styles.css';

const container = document.getElementById('root');
if (!container) throw new Error('#root is missing from index.html');

createRoot(container).render(<App />);

// A visible, honest failure beats a frozen canvas with nothing in the console.
window.addEventListener('unhandledrejection', (event) => {
  console.error('Unhandled rejection:', event.reason);
});
