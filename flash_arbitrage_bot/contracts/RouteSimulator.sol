// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title RouteSimulator
/// @notice Never deployed. The bot places this code at an unused address with an
/// eth_call state override and calls `simulate`. It flash-borrows `amount` of
/// `token` from the Balancer vault, swaps through `steps` exactly the way
/// FlashArbitrage does, records what actually arrived after every hop, and then
/// always reverts with those numbers. Nothing is ever committed and no funds
/// are needed, so scan mode gets an exact check of the whole trade: real pool
/// fees, real liquidity depth, transfer taxes and the flash-loan fee included.
///
/// Result (as revert data):
///   SimResult(flashFee, received[], reported[])
///     received[i] = balance increase of steps[i].tokenOut (after any transfer tax)
///     reported[i] = amount the router says it sent (0 for routers that return nothing)
///   HopFailed(hop, reason) if a swap reverted (reason = the router's revert data)

interface IERC20 {
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
}

interface IBalancerVault {
    function flashLoan(address recipient, address[] memory tokens, uint256[] memory amounts, bytes memory userData)
        external;
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

contract RouteSimulator {
    /// Same layout and kinds as FlashArbitrage.Step.
    struct Step {
        uint8 kind;
        address router;
        address tokenIn;
        address tokenOut;
        uint24 fee;
    }

    uint8 internal constant KIND_V2 = 0;
    uint8 internal constant KIND_V3_ROUTER02 = 1;
    uint8 internal constant KIND_V3_ROUTER = 2;
    uint8 internal constant KIND_CAMELOT_V2 = 3;
    uint8 internal constant KIND_ALGEBRA = 4;

    error SimResult(uint256 flashFee, uint256[] received, uint256[] reported);
    error HopFailed(uint256 hop, bytes reason);
    error BadRoute();
    error OnlySelf();
    error TokenCallFailed(address token);

    function simulate(address vault, address token, uint256 amount, Step[] calldata steps) external {
        if (steps.length < 2 || steps[0].tokenIn != token || steps[steps.length - 1].tokenOut != token) {
            revert BadRoute();
        }
        address[] memory tokens = new address[](1);
        tokens[0] = token;
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = amount;
        IBalancerVault(vault).flashLoan(address(this), tokens, amounts, abi.encode(steps));
    }

    /// Balancer V2 flash-loan callback: run the swaps, then revert with the results.
    function receiveFlashLoan(
        address[] calldata,
        uint256[] calldata,
        uint256[] calldata feeAmounts,
        bytes calldata userData
    ) external {
        Step[] memory steps = abi.decode(userData, (Step[]));
        uint256 n = steps.length;
        uint256[] memory received = new uint256[](n);
        uint256[] memory reported = new uint256[](n);
        for (uint256 i = 0; i < n; i++) {
            uint256 before = IERC20(steps[i].tokenOut).balanceOf(address(this));
            try this.swapStep(steps[i]) returns (uint256 sent) {
                reported[i] = sent;
            } catch (bytes memory reason) {
                revert HopFailed(i, reason);
            }
            received[i] = IERC20(steps[i].tokenOut).balanceOf(address(this)) - before;
        }
        revert SimResult(feeAmounts[0], received, reported);
    }

    /// One swap of this contract's whole `tokenIn` balance, as in FlashArbitrage._swap.
    /// External (self-call only) so a failure can be caught and tagged with its hop.
    function swapStep(Step calldata s) external returns (uint256) {
        if (msg.sender != address(this)) revert OnlySelf();
        uint256 amountIn = IERC20(s.tokenIn).balanceOf(address(this));
        _call(s.tokenIn, abi.encodeCall(IERC20.approve, (s.router, amountIn)));

        if (s.kind == KIND_V2) {
            address[] memory path = new address[](2);
            path[0] = s.tokenIn;
            path[1] = s.tokenOut;
            uint256[] memory amounts = IUniswapV2Router(s.router).swapExactTokensForTokens(
                amountIn, 0, path, address(this), block.timestamp
            );
            return amounts[amounts.length - 1];
        } else if (s.kind == KIND_V3_ROUTER02) {
            return IV3SwapRouter02(s.router).exactInputSingle(
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
            return IV3SwapRouter(s.router).exactInputSingle(
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
            return 0;
        } else if (s.kind == KIND_ALGEBRA) {
            return IAlgebraSwapRouter(s.router).exactInputSingle(
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
        }
        revert BadRoute();
    }

    // Low-level call so tokens that return nothing (e.g. USDT) also work.
    function _call(address token, bytes memory data) private {
        (bool ok, bytes memory ret) = token.call(data);
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert TokenCallFailed(token);
    }
}
