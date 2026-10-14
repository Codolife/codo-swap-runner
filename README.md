# codo-swap-runner

הרץ שמבצע החלפות מנחה במפגשי קודו אונליין, 24/7, חינם, על GitHub Actions.

כל כ-15 דקות: מושך בקשות ממתינות מהתור (Apps Script), מבצע את ההחלפה ב-Arbox
דרך דפדפן headless (הדבר היחיד שעובר את חומת הכתיבה של Arbox), מוודא מול ה-API
הציבורי, ומחזיר ל-Apps Script לעדכון היומן וסימון בוצע.

## סודות (Settings → Secrets and variables → Actions)
- `ARBOX_JWT` — טוקן Arbox של תמר (אישי, רגיש).
- `ARBOX_API_KEY` — מפתח הקריאה הציבורי של Arbox.
- `APPS_SCRIPT_URL` — כתובת אפליקציית ה-Web של Apps Script (התור והסגירה).
- `SHARED_SECRET` — מחרוזת משותפת בין הרץ ל-Apps Script.

## בטיחות (נעולה ב-process.py)
רק updateEventStaff על משבצת קיימת. רק קודו אונליין / CoWork / סופ"ש במיקום 19747.
בודק אחרי כל כתיבה. אידמפוטנטי. כישלון בשורה אחת מסומן error וממשיך לשאר.
לא מוחק, לא מבטל, לא שולח דבר מעבר להזמנת היומן.

## הרצה ידנית
לשונית Actions → process-swaps → Run workflow.
