// An .html file imports as its text (the "Text" module rule in wrangler.jsonc).
declare module "*.html" {
  const text: string;
  export default text;
}
