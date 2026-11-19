package com.svetool.monitor

import android.content.Intent
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.text.InputType
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

/**
 * 服务器地址配置页（首次启动引导 & 运行时切换服务器）。
 */
class ServerSetupActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_ALLOW_CANCEL = "allow_cancel"
    }

    private lateinit var input: EditText
    private lateinit var errorText: TextView
    private var allowCancel: Boolean = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val currentUrl = ServerConfig.getBaseUrl(this)
        allowCancel = intent.getBooleanExtra(EXTRA_ALLOW_CANCEL, currentUrl.isNotEmpty())

        val dp = { v: Int ->
            TypedValue.applyDimension(
                TypedValue.COMPLEX_UNIT_DIP,
                v.toFloat(),
                resources.displayMetrics
            ).toInt()
        }

        val scroll = ScrollView(this).apply {
            setBackgroundColor(Color.parseColor("#0f1116"))
            isFillViewport = true
        }

        val outer = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(20), dp(24), dp(20), dp(24))
        }

        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(22), dp(20), dp(22))
            background = GradientDrawable().apply {
                setColor(Color.parseColor("#171a21"))
                cornerRadius = dp(12).toFloat()
                setStroke(dp(1), Color.parseColor("#2a2f3a"))
            }
        }

        val title = TextView(this).apply {
            text = getString(R.string.server_setup_title)
            setTextColor(Color.parseColor("#dfe3ea"))
            textSize = 20f
            setPadding(0, 0, 0, dp(8))
        }
        card.addView(title)

        val hint = TextView(this).apply {
            text = getString(R.string.server_setup_hint)
            setTextColor(Color.parseColor("#8b93a3"))
            textSize = 13f
            setPadding(0, 0, 0, dp(16))
        }
        card.addView(hint)

        input = EditText(this).apply {
            this.hint = getString(R.string.server_setup_placeholder)
            setTextColor(Color.parseColor("#dfe3ea"))
            setHintTextColor(Color.parseColor("#6b7280"))
            textSize = 15f
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            imeOptions = EditorInfo.IME_ACTION_DONE
            isSingleLine = true
            setPadding(dp(14), dp(12), dp(14), dp(12))
            background = GradientDrawable().apply {
                setColor(Color.parseColor("#1d2129"))
                cornerRadius = dp(8).toFloat()
                setStroke(dp(1), Color.parseColor("#2a2f3a"))
            }
            if (currentUrl.isNotEmpty()) {
                setText(currentUrl)
                setSelection(currentUrl.length)
            }
        }
        card.addView(
            input,
            LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            )
        )

        errorText = TextView(this).apply {
            setTextColor(Color.parseColor("#f87171"))
            textSize = 13f
            visibility = View.GONE
            setPadding(0, dp(10), 0, 0)
        }
        card.addView(errorText)

        val saveBtn = Button(this).apply {
            text = getString(R.string.server_setup_save)
            isAllCaps = false
            setTextColor(Color.parseColor("#06121f"))
            textSize = 15f
            setPadding(dp(16), dp(12), dp(16), dp(12))
            background = GradientDrawable().apply {
                setColor(Color.parseColor("#3b82f6"))
                cornerRadius = dp(8).toFloat()
            }
            setOnClickListener { submit() }
        }
        card.addView(
            saveBtn,
            LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply {
                topMargin = dp(18)
            }
        )

        if (allowCancel) {
            val cancelBtn = Button(this).apply {
                text = getString(R.string.server_setup_cancel)
                isAllCaps = false
                setTextColor(Color.parseColor("#dfe3ea"))
                textSize = 14f
                setPadding(dp(16), dp(10), dp(16), dp(10))
                background = GradientDrawable().apply {
                    setColor(Color.parseColor("#1d2129"))
                    cornerRadius = dp(8).toFloat()
                    setStroke(dp(1), Color.parseColor("#2a2f3a"))
                }
                setOnClickListener {
                    setResult(RESULT_CANCELED)
                    finish()
                }
            }
            card.addView(
                cancelBtn,
                LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT
                ).apply {
                    topMargin = dp(10)
                }
            )
        }

        input.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_DONE) {
                submit()
                true
            } else {
                false
            }
        }

        outer.addView(
            card,
            LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            )
        )
        scroll.addView(outer)
        setContentView(scroll)
    }

    private fun submit() {
        when (val result = ServerConfig.setBaseUrl(this, input.text.toString())) {
            is ServerUrlValidator.ValidationResult.Invalid -> {
                errorText.text = result.error
                errorText.visibility = View.VISIBLE
            }
            is ServerUrlValidator.ValidationResult.Valid -> {
                errorText.visibility = View.GONE
                if (allowCancel) {
                    setResult(RESULT_OK)
                    finish()
                } else {
                    startActivity(
                        Intent(this, MainActivity::class.java).apply {
                            addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
                        }
                    )
                    finish()
                }
            }
        }
    }
}
