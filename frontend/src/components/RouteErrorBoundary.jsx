import { Component } from "react";

import ErrorState from "./ErrorState.jsx";

/**
 * Contains a render-time crash to the routed page content ONLY.
 *
 * Before this existed, App.jsx had NO error boundary anywhere: TopBar
 * and Sidebar are siblings of <Routes> under the same root div
 * (app-shell), so an uncaught exception thrown while rendering any
 * page (e.g. a malformed WebSocket push producing a value a column
 * renderer doesn't expect) unmounted React's entire tree -- nav bars
 * included -- leaving a blank, totally unresponsive app with no error
 * shown ("no nav bars or working"). Wrapping just <Suspense><Routes>
 * in AppShell with this boundary means a crash anywhere in a page's
 * render is caught there: the sidebar/topbar stay mounted and
 * clickable, so the user can navigate to another page to recover
 * instead of reloading blind.
 *
 * key={pathname} on the usage site (see App.jsx) resets this
 * boundary's `hasError` state on every route change -- otherwise,
 * once tripped, it would keep showing the crashed page's error even
 * after navigating elsewhere, since an error boundary's own state
 * never clears itself.
 */
export default class RouteErrorBoundary extends Component {
  state = { hasError: false };

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  componentDidCatch(error, info) {
    console.error("Route render error:", error, info?.componentStack);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="panel">
          <ErrorState
            title="This page hit an unexpected error"
            detail="The rest of the app is still working -- use the sidebar to navigate elsewhere, or reload this page."
          />
        </div>
      );
    }
    return this.props.children;
  }
}
