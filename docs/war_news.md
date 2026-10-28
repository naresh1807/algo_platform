# War and geopolitical news

Open `/news` and choose **War & geopolitical news**. The authenticated `/api/news/war-headlines/` endpoint fetches a dedicated public Google News RSS search, filters by geopolitical keywords and the last 24 hours, removes duplicates, rejects future publications and unsafe links, and caches the result for five minutes. The frontend refreshes every five minutes while the tab is mounted.

The initial fetch returned 30 sourced headlines. A review snapshot with publisher links and publication dates is stored in `logs/war_headlines.json`. Raw headlines are not FinBERT-scored and are not training labels or order instructions. The existing broad market-news pipeline retains its geopolitical classification. No model, trading mode or order-execution setting was changed.

Verification: three focused feed tests and frontend production build passed. Publisher coverage and keyword matching can miss or misclassify stories; an empty feed is displayed honestly rather than populated with examples.
