// The app's first chunk, which main.tsx loads beside React DOM's.
export { App } from "./app/App";
export { preloadView } from "./router/views";
// Last, after every component's stylesheet, as when one bundle held them all:
// the base styles win a tie with a component's.
import "./styles.css";
