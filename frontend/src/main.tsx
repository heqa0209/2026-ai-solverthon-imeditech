import React from "react";
import ReactDOM from "react-dom/client";

import "./styles.css";

function Bootstrap() {
  return <main><h1>기업지원 공고 판정</h1></main>;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode><Bootstrap /></React.StrictMode>,
);
