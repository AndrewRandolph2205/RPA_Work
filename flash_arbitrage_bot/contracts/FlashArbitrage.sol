// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title FlashArbitrage
/// @notice Borrows one token from the Balancer V2 vault (flash loan), swaps it
/// through a cycle of DEX pools back into the same token, repays the loan and
/// sends the profit to the owner, all in one transaction.
///
/// Safety properties:
/// - The whole transaction reverts unless the final balance covers the loan,
///   the loan fee and `minProfit`. An unprofitable attempt can only cost gas.
/// - Only the owner can start a trade.
/// - The flash-loan callback only runs for the exact loan this contract just
///   requested, once, so nobody else can trigger swaps through it.
/// - The contract keeps no funds: profit is sent to the owner immediately.

interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
}

interface IBalancerVault {
    function flashLoan(
        address recipient,
        address[] memory tokens,
        uint256[] memory amounts,
        bytes memory userData
    ) external;
}

interface IUniswapV2Router {
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);
}

/// Uniswap SwapRouter02 style: no deadline field.
interface IV3SwapRouter02 {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256);
}

/// Original Uniswap V3 SwapRouter style: has a deadline field.
interface IV3SwapRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256);
}

/// Camelot V2 router: V2-style, but takes a referrer and returns nothing.
interface ICamelotRouter {
    function swapExactTokensForTokensSupportingFeeOnTransferTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        address referrer,
        uint256 deadline
    ) external;
}

/// Solidly / Velodrome V2 router (Aerodrome on Base). factory = address(0) means the
/// router's default factory; only volatile (stable = false) pools are used.
interface ISolidlyRouter {
    struct Route {
        address from;
        address to;
        bool stable;
        address factory;
    }

    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        Route[] calldata routes,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);
}

/// Algebra swap router (Camelot V3): no fee field, one pool per pair.
interface IAlgebraSwapRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 limitSqrtPrice;
    }

    function exactInputSingle(ExactInputSingleParams calldata params) external payable returns (uint256);
}

contract FlashArbitrage {
    struct Step {
        uint8 kind; // 0 = Uniswap V2, 1 = V3 SwapRouter02, 2 = V3 SwapRouter, 3 = Camelot V2, 4 = Algebra, 5 = Solidly
        address router;
        address tokenIn;
        address tokenOut;
        uint24 fee; // V3 pool fee tier; ignored for V2
    }

    uint8 internal constant KIND_V2 = 0;
    uint8 internal constant KIND_V3_ROUTER02 = 1;
    uint8 internal constant KIND_V3_ROUTER = 2;
    uint8 internal constant KIND_CAMELOT_V2 = 3;
    uint8 internal constant KIND_ALGEBRA = 4;
    uint8 internal constant KIND_SOLIDLY = 5;

    address public immutable owner;
    IBalancerVault public immutable vault;

    /// keccak256 of the userData of the loan we requested; cleared on use.
    bytes32 private pendingLoan;

    event Arbitrage(address indexed token, uint256 amount, uint256 profit);

    error NotOwner();
    error NotVault();
    error UnexpectedLoan();
    error BadRoute();
    error Unprofitable(uint256 balance, uint256 required);
    error TokenCallFailed(address token);

    constructor(address vault_) {
        owner = msg.sender;
        vault = IBalancerVault(vault_);
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    /// @param token     token to borrow; the route must start and end with it
    /// @param amount    amount to borrow
    /// @param steps     swaps to perform, in order
    /// @param minProfit revert unless at least this much `token` is left after repaying
    function execute(address token, uint256 amount, Step[] calldata steps, uint256 minProfit)
        external
        onlyOwner
    {
        uint256 n = steps.length;
        if (n < 2 || steps[0].tokenIn != token || steps[n - 1].tokenOut != token) revert BadRoute();
        for (uint256 i = 1; i < n; i++) {
            if (steps[i].tokenIn != steps[i - 1].tokenOut) revert BadRoute();
        }

        address[] memory tokens = new address[](1);
        tokens[0] = token;
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = amount;

        bytes memory data = abi.encode(steps, minProfit);
        pendingLoan = keccak256(data);
        vault.flashLoan(address(this), tokens, amounts, data);
    }

    /// Balancer V2 flash-loan callback.
    function receiveFlashLoan(
        address[] calldata tokens,
        uint256[] calldata amounts,
        uint256[] calldata feeAmounts,
        bytes calldata userData
    ) external {
        if (msg.sender != address(vault)) revert NotVault();
        if (tokens.length != 1 || pendingLoan == bytes32(0) || keccak256(userData) != pendingLoan) {
            revert UnexpectedLoan();
        }
        pendingLoan = bytes32(0);

        (Step[] memory steps, uint256 minProfit) = abi.decode(userData, (Step[], uint256));
        address token = tokens[0];
        uint256 owed = amounts[0] + feeAmounts[0];

        for (uint256 i = 0; i < steps.length; i++) {
            _swap(steps[i]);
        }

        uint256 balance = IERC20(token).balanceOf(address(this));
        if (balance < owed + minProfit) revert Unprofitable(balance, owed + minProfit);

        _transfer(token, address(vault), owed);
        uint256 profit = balance - owed;
        if (profit > 0) _transfer(token, owner, profit);
        emit Arbitrage(token, amounts[0], profit);
    }

    /// Recover any token accidentally left in or sent to this contract.
    function withdraw(address token) external onlyOwner {
        _transfer(token, owner, IERC20(token).balanceOf(address(this)));
    }

    /// Swaps this contract's whole balance of `s.tokenIn`. The per-hop minimum
    /// output is 0 because profitability is enforced once, at the end.
    function _swap(Step memory s) private {
        uint256 amountIn = IERC20(s.tokenIn).balanceOf(address(this));
        _approve(s.tokenIn, s.router, amountIn);

        if (s.kind == KIND_V2) {
            address[] memory path = new address[](2);
            path[0] = s.tokenIn;
            path[1] = s.tokenOut;
            IUniswapV2Router(s.router).swapExactTokensForTokens(amountIn, 0, path, address(this), block.timestamp);
        } else if (s.kind == KIND_V3_ROUTER02) {
            IV3SwapRouter02(s.router).exactInputSingle(
                IV3SwapRouter02.ExactInputSingleParams({
                    tokenIn: s.tokenIn,
                    tokenOut: s.tokenOut,
                    fee: s.fee,
                    recipient: address(this),
                    amountIn: amountIn,
                    amountOutMinimum: 0,
                    sqrtPriceLimitX96: 0
                })
            );
        } else if (s.kind == KIND_V3_ROUTER) {
            IV3SwapRouter(s.router).exactInputSingle(
                IV3SwapRouter.ExactInputSingleParams({
                    tokenIn: s.tokenIn,
                    tokenOut: s.tokenOut,
                    fee: s.fee,
                    recipient: address(this),
                    deadline: block.timestamp,
                    amountIn: amountIn,
                    amountOutMinimum: 0,
                    sqrtPriceLimitX96: 0
                })
            );
        } else if (s.kind == KIND_CAMELOT_V2) {
            address[] memory path = new address[](2);
            path[0] = s.tokenIn;
            path[1] = s.tokenOut;
            ICamelotRouter(s.router).swapExactTokensForTokensSupportingFeeOnTransferTokens(
                amountIn, 0, path, address(this), address(0), block.timestamp
            );
        } else if (s.kind == KIND_ALGEBRA) {
            IAlgebraSwapRouter(s.router).exactInputSingle(
                IAlgebraSwapRouter.ExactInputSingleParams({
                    tokenIn: s.tokenIn,
                    tokenOut: s.tokenOut,
                    recipient: address(this),
                    deadline: block.timestamp,
                    amountIn: amountIn,
                    amountOutMinimum: 0,
                    limitSqrtPrice: 0
                })
            );
        } else if (s.kind == KIND_SOLIDLY) {
            ISolidlyRouter.Route[] memory routes = new ISolidlyRouter.Route[](1);
            routes[0] = ISolidlyRouter.Route({from: s.tokenIn, to: s.tokenOut, stable: false, factory: address(0)});
            ISolidlyRouter(s.router).swapExactTokensForTokens(amountIn, 0, routes, address(this), block.timestamp);
        } else {
            revert BadRoute();
        }
    }

    // Low-level calls so tokens that return nothing (e.g. USDT) also work.
    function _approve(address token, address spender, uint256 amount) private {
        _call(token, abi.encodeCall(IERC20.approve, (spender, amount)));
    }

    function _transfer(address token, address to, uint256 amount) private {
        _call(token, abi.encodeCall(IERC20.transfer, (to, amount)));
    }

    function _call(address token, bytes memory data) private {
        (bool ok, bytes memory ret) = token.call(data);
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert TokenCallFailed(token);
    }
}
