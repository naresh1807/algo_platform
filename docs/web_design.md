# Web design and mobile navigation

The interface now uses a consistent desktop and phone layout.

- Home shows the market watch, trading shortcuts, and account summaries.
- Desktop navigation groups Workspace, Markets, Strategies & AI, and Account insights.
- Phone navigation uses Home, Trade, Orders, Portfolio, and More.
- More opens a native modal with keyboard focus containment and Escape support.
- The header offers page/tool search, also available with Ctrl+K or Cmd+K.
- Orders shows the current user's manual option request history, status filters, contract search, and mobile detail cards.
- Portfolio separates open and closed trading positions, filters paper/live mode, and shows net P&L. Summary values cover the loaded records.
- Both light and dark themes remain available. Sign-in has labeled inputs, password visibility, and returns to the requested page after authentication.

The running development website is at http://localhost:3000. Refresh the browser to see the changes. The redesign uses existing data and trading APIs.

Validation uses isolated browser fixtures, not real orders. Browser checks cover 320, 390, 740, and 1440 pixel viewports, route navigation, search, mobile cards, Escape, modal focus containment, dark mode, and password visibility. Test screenshots and the browser script are under logs/design-preview.

