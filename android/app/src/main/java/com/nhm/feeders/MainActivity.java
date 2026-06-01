package com.nhm.feeders;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.ProgressBar;

import androidx.core.content.FileProvider;

import java.io.File;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * Thin WebView wrapper around the NHM Feeders web app. Loads the site, keeps
 * the session cookie, supports <input type="file"> with camera + gallery (so
 * site photos work), and uses the device back button to navigate history.
 *
 * To change the site URL, edit @string/app_url in res/values/strings.xml.
 */
public class MainActivity extends Activity {

    private WebView webView;
    private ProgressBar progress;
    private ValueCallback<Uri[]> filePathCallback;
    private Uri cameraImageUri;

    private static final int REQ_FILE = 1001;
    private static final int REQ_PERMS = 2001;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        webView = findViewById(R.id.webview);
        progress = findViewById(R.id.progress);

        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setAllowFileAccess(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setBuiltInZoomControls(true);
        s.setDisplayZoomControls(false);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setJavaScriptCanOpenWindowsAutomatically(true);

        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, true);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String url = request.getUrl().toString();
                if (url.startsWith("http://") || url.startsWith("https://")) {
                    return false; // keep normal navigation inside the app
                }
                try {                       // tel:, mailto:, etc. -> system handler
                    startActivity(new Intent(Intent.ACTION_VIEW, request.getUrl()));
                } catch (Exception ignored) {
                }
                return true;
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int newProgress) {
                progress.setProgress(newProgress);
                progress.setVisibility(newProgress < 100 ? View.VISIBLE : View.GONE);
            }

            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                runOnUiThread(() -> request.grant(request.getResources()));
            }

            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> cb,
                                             FileChooserParams params) {
                if (filePathCallback != null) {
                    filePathCallback.onReceiveValue(null);
                }
                filePathCallback = cb;
                openFileChooser(params);
                return true;
            }
        });

        if (savedInstanceState != null) {
            webView.restoreState(savedInstanceState);
        } else {
            webView.loadUrl(getString(R.string.app_url));
        }

        requestRuntimePermissions();
    }

    private void requestRuntimePermissions() {
        if (Build.VERSION.SDK_INT >= 23
                && checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.CAMERA}, REQ_PERMS);
        }
    }

    private void openFileChooser(WebChromeClient.FileChooserParams params) {
        Intent content = params.createIntent();              // honours the page's accept types
        content.addCategory(Intent.CATEGORY_OPENABLE);

        Intent camera = null;
        if (hasCamera() && cameraPermissionGranted()) {
            File photo = createImageFile();
            if (photo != null) {
                cameraImageUri = FileProvider.getUriForFile(
                        this, getPackageName() + ".fileprovider", photo);
                camera = new Intent(MediaStore.ACTION_IMAGE_CAPTURE)
                        .putExtra(MediaStore.EXTRA_OUTPUT, cameraImageUri);
            }
        }

        Intent chooser = Intent.createChooser(content, "Select or take a photo");
        if (camera != null) {
            chooser.putExtra(Intent.EXTRA_INITIAL_INTENTS, new Intent[]{camera});
        }
        try {
            startActivityForResult(chooser, REQ_FILE);
        } catch (Exception e) {
            if (filePathCallback != null) {
                filePathCallback.onReceiveValue(null);
                filePathCallback = null;
            }
        }
    }

    private boolean hasCamera() {
        return getPackageManager().hasSystemFeature(PackageManager.FEATURE_CAMERA_ANY);
    }

    private boolean cameraPermissionGranted() {
        return Build.VERSION.SDK_INT < 23
                || checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED;
    }

    private File createImageFile() {
        try {
            String name = "IMG_" + new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(new Date());
            File dir = getExternalFilesDir(Environment.DIRECTORY_PICTURES);
            if (dir != null && !dir.exists()) {
                dir.mkdirs();
            }
            return File.createTempFile(name, ".jpg", dir);
        } catch (Exception e) {
            return null;
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_FILE || filePathCallback == null) {
            return;
        }
        Uri[] results = null;
        if (resultCode == RESULT_OK) {
            if (data != null && (data.getData() != null || data.getClipData() != null)) {
                results = WebChromeClient.FileChooserParams.parseResult(resultCode, data);
            } else if (cameraImageUri != null) {
                results = new Uri[]{cameraImageUri};   // photo taken with the camera
            }
        }
        filePathCallback.onReceiveValue(results);
        filePathCallback = null;
        cameraImageUri = null;
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        webView.saveState(outState);
    }
}
