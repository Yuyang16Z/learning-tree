import { Component, type ReactNode } from "react";

interface Props {
  /** A new value (the open topic and node, say) shows the children again after a failure. */
  resetKey: string;
  fallback: ReactNode;
  children: ReactNode;
}

/** Keeps a rendering failure inside one pane from unmounting the whole app; React logs the error. */
export class ErrorBoundary extends Component<Props, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidUpdate(previous: Props) {
    if (this.state.failed && previous.resetKey !== this.props.resetKey) this.setState({ failed: false });
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}
