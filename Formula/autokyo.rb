class Autokyo < Formula
  include Language::Python::Virtualenv

  desc "macOS local automation tool for page-by-page ebook viewer workflows"
  homepage "https://github.com/plain127/homebrew-autokyo"
  url "https://github.com/plain127/homebrew-autokyo/archive/refs/tags/v0.1.3.tar.gz"
  sha256 "9d6f3af39b9ef37e8e935537dac268c256f8994d9cf8d472da914684f294cdf4"

  depends_on "python@3.12"

  def install
    virtualenv_install_with_resources
  end

  def post_install
    system bin/"autokyo", "init-config"
  end

  def caveats
    <<~EOS
      Default config created at:
        #{Dir.home}/Library/Application Support/AutoKyo/config.toml

      Edit this file before running `autokyo run` or `autokyo mcp-install ...`.
    EOS
  end

  test do
    assert_match "autokyo", shell_output("#{bin}/autokyo --help")
  end
end
