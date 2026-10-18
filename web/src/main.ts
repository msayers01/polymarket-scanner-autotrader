import { mount } from "svelte";
import "./app.css";
import App from "./App.svelte";
import { connect } from "./lib/store.svelte";

connect();

export default mount(App, { target: document.getElementById("app")! });
