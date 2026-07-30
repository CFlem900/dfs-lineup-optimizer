import React from 'react';
import { AlertTriangle, RefreshCw } from 'lucide-react';

class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null, errorInfo: null };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, errorInfo) {
    this.setState({ errorInfo });
    console.error('[ErrorBoundary] Caught error:', error, errorInfo);
    if (this.props.onError) {
      this.props.onError(error, errorInfo);
    }
  }

  handleRetry = () => {
    this.setState({ hasError: false, error: null, errorInfo: null });
  };

  render() {
    if (this.state.hasError) {
      // Custom fallback
      if (this.props.fallback) {
        return this.props.fallback({
          error: this.state.error,
          retry: this.handleRetry,
        });
      }

      // Default fallback UI
      return (
        <div className="flex flex-col items-center justify-center p-6 bg-gray-900/50 border border-red-500/30 rounded-lg m-2">
          <AlertTriangle className="w-10 h-10 text-red-400 mb-3" />
          <h3 className="text-red-300 font-semibold text-sm mb-1">
            Something went wrong
          </h3>
          <p className="text-gray-400 text-xs text-center mb-3 max-w-md">
            {this.state.error?.message || 'An unexpected error occurred in this section.'}
          </p>
          <button
            onClick={this.handleRetry}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-red-500/20 hover:bg-red-500/30
                       text-red-300 text-xs rounded border border-red-500/30 transition-colors"
          >
            <RefreshCw className="w-3 h-3" />
            Retry
          </button>
          {process.env.NODE_ENV === 'development' && this.state.errorInfo && (
            <details className="mt-3 w-full max-w-lg">
              <summary className="text-gray-500 text-xs cursor-pointer hover:text-gray-400">
                Error details
              </summary>
              <pre className="mt-1 p-2 bg-gray-900 rounded text-xs text-gray-500 overflow-auto max-h-40">
                {this.state.error?.stack}
                {'\n\nComponent Stack:'}
                {this.state.errorInfo?.componentStack}
              </pre>
            </details>
          )}
        </div>
      );
    }

    return this.props.children;
  }
}

export default ErrorBoundary;
